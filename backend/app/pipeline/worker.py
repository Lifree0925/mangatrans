"""持久 Worker：Draft/Polish 流水线并行（各自窗口级并发）。

- carry-over 由 Draft 阶段顺带生成并写入任务进度（调度层顺序写），
  Polish 消费前一窗口的 Draft carry。
- 无待定术语时，同一轮可同时领取 Draft 与依赖就绪的 Polish；有待定术语时
  先等全量 Draft 完成并锁名，再启动 Polish，保证术语一致性。
- started_at 超时回收：崩溃/僵死后自动回 pending，断点续跑。
"""

import asyncio
import os
import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app.core.database as dbmod
from app.models.task import Task, TaskItem
from app.pipeline.task_timer import (
    settle_task_timer,
    settle_task_timer_for_recovery,
    start_task_timer,
)
from app.services.translation_batch import (
    process_window_draft_item,
    process_window_polish_item,
    process_window_qa_item,
)
from app.llm.usage import usage_context_for_task

LEASE_TIMEOUT_MINUTES = 30
POLL_SECONDS = 1.0
WINDOW_TIMEOUT_SECONDS = 240  # 单窗口硬超时：思考模式慢调用兜底（截断已有降级重试，240s 足够）
QA_TIMEOUT_SECONDS = 600  # QA 定点修复最多 3 轮润色请求：超时只重跑 QA 节点，不碰窗口翻译
WINDOW_CONCURRENCY = max(1, int(os.environ.get("MT_WINDOW_CONCURRENCY", "3")))
DRAFT_CONCURRENCY = WINDOW_CONCURRENCY
POLISH_CONCURRENCY = WINDOW_CONCURRENCY
QA_CONCURRENCY = WINDOW_CONCURRENCY


def recover_stale_items(db) -> int:
    """启动时回收：running 且 started_at 超时且无成功结果 → pending。"""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LEASE_TIMEOUT_MINUTES)
    rows = list(
        db.scalars(
            select(TaskItem)
            .join(Task, TaskItem.task_id == Task.id)
            .where(
                TaskItem.status == "running",
                TaskItem.started_at.is_not(None),
                TaskItem.started_at < cutoff,
                Task.status.in_(["running", "queued", "paused"]),
            )
        )
    )
    for item in rows:
        item.status = "pending"
    db.commit()
    return len(rows)


def recover_all_running(db) -> int:
    """进程启动时回收全部 running 项（单机单 Worker：进程重启后不存在
    正在执行的窗口，running 全部是上次进程的残留，无需等 30 分钟超时）。"""
    rows = list(
        db.scalars(
            select(TaskItem)
            .join(Task, TaskItem.task_id == Task.id)
            .where(
                TaskItem.status == "running",
                Task.status.in_(["running", "queued", "paused"]),
            )
        )
    )
    for item in rows:
        item.status = "pending"
        item.finished_at = None
    # 上次进程运行段只结算到最后一次写库时间，应用关闭期间的时长不计入。
    active_tasks = list(
        db.scalars(
            select(Task).where(
                Task.status.in_(["running", "queued", "paused"]),
                Task.started_at.is_not(None),
            )
        )
    )
    for task in active_tasks:
        settle_task_timer_for_recovery(task)
    db.commit()
    return len(rows)


def _task_usable(db, task_id: str) -> bool:
    """任务级暂停/取消检查。"""
    task = db.get(Task, task_id)
    return task is not None and task.status not in ("paused", "failed", "cancelled")


def _model_of(db, task_id: str) -> str:
    payload = json.loads(db.get(Task, task_id).config_snapshot_json or "{}")
    return payload.get("model", "fake-model")


def claim_draft_batch(db, limit: int = DRAFT_CONCURRENCY) -> list[TaskItem]:
    """原子领取最多 limit 个 draft 待办（并发执行；只领取活动任务的窗口）。"""
    rows = list(
        db.scalars(
            select(TaskItem)
            .join(Task, TaskItem.task_id == Task.id)
            .where(
                TaskItem.status == "pending",
                TaskItem.phase == "draft",
                Task.status.in_(["running", "queued"]),
            )
            .order_by(TaskItem.window_id)
            .limit(limit)
        )
    )
    claimed: list[TaskItem] = []
    for item in rows:
        if not _task_usable(db, item.task_id):
            continue
        task = db.get(Task, item.task_id)
        if task is not None:
            if task.status == "queued":
                task.status = "running"
            start_task_timer(task)
        item.status = "running"
        item.started_at = datetime.now(timezone.utc)
        item.attempts += 1
        claimed.append(item)
    db.commit()
    return claimed


def claim_polish_batch(db, limit: int = POLISH_CONCURRENCY) -> list[TaskItem]:
    """领取依赖就绪的 polish 窗口：术语已锁定，且前一窗口 Draft 已完成。"""
    rows = list(
        db.scalars(
            select(TaskItem)
            .join(Task, TaskItem.task_id == Task.id)
            .where(
                TaskItem.status == "pending",
                TaskItem.phase == "polish",
                Task.status.in_(["running", "queued"]),
            )
            .order_by(TaskItem.window_id)
        )
    )
    terms_ready: dict[str, bool] = {}
    claimed: list[TaskItem] = []
    for item in rows:
        if len(claimed) >= limit:
            break
        if not _task_usable(db, item.task_id):
            continue
        if item.task_id not in terms_ready:
            task = db.get(Task, item.task_id)
            progress = json.loads((task.progress_json if task else "") or "{}")
            terms_ready[item.task_id] = bool(progress.get("terms_locked"))
        if not terms_ready[item.task_id]:
            continue
        try:
            win_idx = int(item.window_id.split("_")[1])
        except (IndexError, ValueError):
            continue
        if win_idx > 0:
            prev_id = f"WIN_{win_idx - 1:04d}"
            prev = db.scalar(
                select(TaskItem).where(
                    TaskItem.task_id == item.task_id,
                    TaskItem.window_id == prev_id,
                )
            )
            # 单 item 只在 Draft 成功后从 draft 转 polish；未完成时不能提前润色。
            if prev is not None and prev.phase == "draft" and prev.status in ("pending", "running"):
                continue
        task = db.get(Task, item.task_id)
        if task is not None:
            if task.status == "queued":
                task.status = "running"
            start_task_timer(task)
        item.status = "running"
        item.started_at = datetime.now(timezone.utc)
        item.attempts += 1
        claimed.append(item)
    db.commit()
    return claimed


def claim_qa_batch(db, limit: int = QA_CONCURRENCY) -> list[TaskItem]:
    """原子领取最多 limit 个 QA 待办（并发执行；只领取活动任务的节点）。"""
    rows = list(
        db.scalars(
            select(TaskItem)
            .join(Task, TaskItem.task_id == Task.id)
            .where(
                TaskItem.status == "pending",
                TaskItem.phase == "qa",
                Task.status.in_(["running", "queued"]),
            )
            .order_by(TaskItem.window_id)
            .limit(limit)
        )
    )
    claimed: list[TaskItem] = []
    for item in rows:
        if not _task_usable(db, item.task_id):
            continue
        task = db.get(Task, item.task_id)
        if task is not None:
            if task.status == "queued":
                task.status = "running"
            start_task_timer(task)
        item.status = "running"
        item.started_at = datetime.now(timezone.utc)
        item.attempts += 1
        claimed.append(item)
    db.commit()
    return claimed


def _mark_failed(
    factory, task_id: str, item_id: str, code: str, *, retryable: bool = True
) -> None:
    """失败处理：只有瞬时错误才自动回 pending；质量门禁/截断/认证等直接失败。"""
    with factory() as db:
        item = db.get(TaskItem, item_id)
        if item is None:
            return
        item.error_code = (code or "error")[:32]
        can_retry = retryable and item.attempts < 3
        if can_retry:
            item.status = "pending"
            item.finished_at = datetime.now(timezone.utc)
        else:
            item.status = "failed"
            item.finished_at = datetime.now(timezone.utc)
            task = db.get(Task, task_id)
            if task is not None:
                progress = json.loads(task.progress_json or "{}")
                progress["failed"] = int(progress.get("failed", 0)) + 1
                task.progress_json = json.dumps(progress, ensure_ascii=False)
        db.commit()


def _apply_draft_carry(factory, task_id: str, results: list) -> None:
    """Draft 批次完成后顺序写入 draft carry map（进程内单协程写，无竞争）。"""
    # 成功窗口都记入（carry 可能为 None，用占位标记 Draft 已完成，前端据此显示草稿进度）
    entries = [
        (r["window_id"], r.get("draft_carry"))
        for r in results
        if isinstance(r, dict) and r.get("window_id")
    ]
    if not entries:
        return
    with factory() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        progress = json.loads(task.progress_json or "{}")
        carry_map = dict(progress.get("draft_carry") or {})
        carry_map.update(entries)
        progress["draft_carry"] = carry_map
        progress["draft_done"] = len(carry_map)
        progress["phase"] = "draft"
        task.progress_json = json.dumps(progress, ensure_ascii=False)
        db.commit()


def _apply_polish_progress(factory, task_id: str, res: dict) -> None:
    """Polish 单窗完成后顺序写任务进度（并发 Polish 下避免读改写竞争）。"""
    with factory() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        progress = json.loads(task.progress_json or "{}")
        progress["carry_over"] = res.get("polish_carry")
        progress["qa_issues"] = int(progress.get("qa_issues", 0)) + int(res.get("qa_issues", 0))
        # done 直接用成功项计数（不用累加）：进程重启/异常中断后进度自动校准
        done = int(
            db.scalar(
                select(func.count())
                .select_from(TaskItem)
                .where(
                    TaskItem.task_id == task_id,
                    TaskItem.status == "succeeded",
                    TaskItem.phase != "qa",
                )
            )
            or 0
        )
        progress["done"] = done
        total = int(progress.get("total", 0)) or db.scalar(
            select(func.count())
            .select_from(TaskItem)
            .where(TaskItem.task_id == task_id, TaskItem.phase != "qa")
        ) or done
        progress["total"] = total
        draft_left = int(
            db.scalar(
                select(func.count())
                .select_from(TaskItem)
                .where(
                    TaskItem.task_id == task_id,
                    TaskItem.phase == "draft",
                    TaskItem.status.in_(["pending", "running"]),
                )
            )
            or 0
        )
        progress["parallel"] = draft_left > 0
        progress["phase"] = "draft" if draft_left else "polish"
        task.progress_json = json.dumps(progress, ensure_ascii=False)
        task.last_checkpoint = res.get("window_id")
        db.commit()


async def _ensure_terms_gate(factory) -> int:
    """术语门控：无待定术语时立即放行流水线；有待定术语时等全量 Draft 后锁定。"""
    from app.llm.registry import get_provider
    from app.services.term_lock import has_pending_untranslated_terms, lock_untranslated_terms

    target: tuple[str, str, str, str] | None = None
    with factory() as db:
        task_ids = list(
            db.scalars(
                select(Task.id)
                .join(TaskItem, TaskItem.task_id == Task.id)
                .where(
                    Task.status.in_(["running", "queued"]),
                    TaskItem.phase.in_(["draft", "polish"]),
                )
                .distinct()
            )
        )
        for tid in task_ids:
            task = db.get(Task, tid)
            if task is None:
                continue
            progress = json.loads(task.progress_json or "{}")
            if progress.get("terms_locked"):
                continue
            if not has_pending_untranslated_terms(db, task.project_id):
                progress["terms_locked"] = {"locked": 0, "pipeline": True}
                task.progress_json = json.dumps(progress, ensure_ascii=False)
                db.commit()
                continue
            draft_left = db.scalar(
                select(func.count())
                .select_from(TaskItem)
                .where(
                    TaskItem.task_id == tid,
                    TaskItem.phase == "draft",
                    TaskItem.status.in_(["pending", "running"]),
                )
            )
            if draft_left:
                continue
            payload = json.loads(task.config_snapshot_json or "{}")
            source_document_id = payload.get("source_document_id")
            if source_document_id:
                target = (
                    tid,
                    task.project_id,
                    source_document_id,
                    payload.get("model", "fake-model"),
                )
                break
    if target is None:
        return 0
    tid, project_id, source_document_id, model = target
    with usage_context_for_task(tid):
        result = await lock_untranslated_terms(
            factory, project_id, source_document_id, get_provider(), model
        )
    with factory() as db:
        task = db.get(Task, tid)
        if task is not None:
            progress = json.loads(task.progress_json or "{}")
            progress["terms_locked"] = {"locked": result.get("locked", 0)}
            task.progress_json = json.dumps(progress, ensure_ascii=False)
            db.commit()
    return int(result.get("locked", 0))


def _apply_qa_progress(factory, task_id: str, res: dict) -> None:
    """QA 节点完成后写任务进度（并发 QA 下单协程顺序写）。"""
    with factory() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        progress = json.loads(task.progress_json or "{}")
        progress["qa_done"] = int(progress.get("qa_done", 0)) + 1
        progress["qa_issues"] = int(progress.get("qa_issues", 0)) + int(res.get("qa_issues", 0))
        progress["phase"] = "qa"
        task.progress_json = json.dumps(progress, ensure_ascii=False)
        db.commit()


def _mark_task_completed(factory, task_id: str) -> None:
    """只有全部节点成功且项目自动质量门禁通过时，任务才允许 completed。"""
    from app.services.quality_service import auto_approve_project, project_quality_report

    with factory() as db:
        task = db.get(Task, task_id)
        if task is None or task.status == "paused":
            return
        active = db.scalar(
            select(func.count())
            .select_from(TaskItem)
            .where(
                TaskItem.task_id == task_id,
                TaskItem.status.in_(["pending", "running"]),
            )
        )
        if active:
            return
        failed_codes = list(
            db.scalars(
                select(TaskItem.error_code).where(
                    TaskItem.task_id == task_id, TaskItem.status == "failed"
                )
            )
        )
        failed = len(failed_codes)
        if failed:
            settle_task_timer(task)
            task.status = "failed"
            if any(code in ("qa_incomplete", "quality_gate") for code in failed_codes):
                task.error_code = "quality_gate"
                task.error_message = (
                    f"自动质检未通过：{failed} 个质检节点失败或仍有严重问题，请重试。"
                )
            else:
                task.error_code = "items_failed"
                task.error_message = f"有 {failed} 个执行节点失败，请重试失败节点。"
            db.commit()
            return
        report = project_quality_report(db, task.project_id)
        if report.get("gate") != "passed":
            settle_task_timer(task)
            task.status = "failed"
            task.error_code = "quality_gate"
            task.error_message = "自动质检未通过：" + str(report.get("reason") or "存在未解决问题")
            progress = json.loads(task.progress_json or "{}")
            progress["quality"] = report
            task.progress_json = json.dumps(progress, ensure_ascii=False)
            db.commit()
            return
        approved = auto_approve_project(db, task.project_id)
        progress = json.loads(task.progress_json or "{}")
        progress["quality"] = report
        progress["approve"] = approved
        task.progress_json = json.dumps(progress, ensure_ascii=False)
        settle_task_timer(task)
        task.status = "completed"
        task.error_code = None
        task.error_message = None
        db.commit()


async def _within_usage(task_id: str, awaitable, timeout: float):
    """为单个窗口任务绑定 LLM 计量上下文。"""
    with usage_context_for_task(task_id):
        return await asyncio.wait_for(awaitable, timeout=timeout)


async def run_once(db_factory=None) -> int:
    """同一轮同时推进 Draft 与依赖就绪的 Polish；QA 在翻译全部收尾后执行。"""
    factory = db_factory or (lambda: dbmod.SessionLocal())

    with factory() as db:
        recover_stale_items(db)
    await _ensure_terms_gate(factory)

    with factory() as db:
        drafts = claim_draft_batch(db)
        draft_jobs = [(it.task_id, it.id, _model_of(db, it.task_id)) for it in drafts]
        polish_items = claim_polish_batch(db)
        polish_jobs = [
            (it.task_id, it.id, _model_of(db, it.task_id)) for it in polish_items
        ]

    if drafts or polish_items:
        draft_await = (
            asyncio.gather(
                *[
                    _within_usage(
                        tid,
                        process_window_draft_item(factory, tid, iid, model),
                        WINDOW_TIMEOUT_SECONDS,
                    )
                    for tid, iid, model in draft_jobs
                ],
                return_exceptions=True,
            )
            if draft_jobs
            else None
        )
        polish_await = (
            asyncio.gather(
                *[
                    _within_usage(
                        tid,
                        process_window_polish_item(factory, tid, iid, model),
                        WINDOW_TIMEOUT_SECONDS,
                    )
                    for tid, iid, model in polish_jobs
                ],
                return_exceptions=True,
            )
            if polish_jobs
            else None
        )
        if draft_await is not None and polish_await is not None:
            draft_results, polish_results = await asyncio.gather(draft_await, polish_await)
        elif draft_await is not None:
            draft_results, polish_results = await draft_await, []
        else:
            draft_results, polish_results = [], await polish_await  # type: ignore[arg-type]

        for (tid, iid, _model), res in zip(draft_jobs, draft_results):
            if isinstance(res, BaseException):
                from app.llm.protocol import LLMError
                code = res.code if isinstance(res, LLMError) else type(res).__name__
                retryable = res.retryable if isinstance(res, LLMError) else True
                _mark_failed(factory, tid, iid, code, retryable=retryable)
            elif isinstance(res, dict) and "error" in res:
                _mark_failed(
                    factory, tid, iid, res.get("error_code") or res["error"],
                    retryable=bool(res.get("retryable", True)),
                )
        for tid in {tid for tid, _i, _m in draft_jobs}:
            _apply_draft_carry(factory, tid, draft_results)

        touched_tasks: set[str] = set()
        for (tid, iid, _model), res in zip(polish_jobs, polish_results):
            touched_tasks.add(tid)
            if isinstance(res, BaseException):
                from app.llm.protocol import LLMError
                code = res.code if isinstance(res, LLMError) else type(res).__name__
                retryable = res.retryable if isinstance(res, LLMError) else True
                _mark_failed(factory, tid, iid, code, retryable=retryable)
            elif isinstance(res, dict) and "error" in res:
                _mark_failed(
                    factory, tid, iid, res.get("error_code") or res["error"],
                    retryable=bool(res.get("retryable", True)),
                )
            else:
                _apply_polish_progress(factory, tid, res)
        for tid in touched_tasks:
            _mark_task_completed(factory, tid)
        return len(draft_jobs) + len(polish_jobs)

    # QA 节点：必须等全部翻译窗口收尾，避免流水线中提前质检。
    with factory() as db:
        translation_left = db.scalar(
            select(func.count())
            .select_from(TaskItem)
            .where(
                TaskItem.phase.in_(["draft", "polish"]),
                TaskItem.status.in_(["pending", "running"]),
            )
        )
        if translation_left:
            return 0
        qa_items = claim_qa_batch(db)
        qa_jobs = [(it.task_id, it.id, _model_of(db, it.task_id)) for it in qa_items]
    if not qa_jobs:
        return 0
    qa_results = await asyncio.gather(
        *[
            _within_usage(
                tid,
                process_window_qa_item(factory, tid, iid, model),
                QA_TIMEOUT_SECONDS,
            )
            for tid, iid, model in qa_jobs
        ],
        return_exceptions=True,
    )
    for (tid, iid, _model), res in zip(qa_jobs, qa_results):
        if isinstance(res, BaseException):
            from app.llm.protocol import LLMError
            code = res.code if isinstance(res, LLMError) else type(res).__name__
            retryable = res.retryable if isinstance(res, LLMError) else True
            _mark_failed(factory, tid, iid, code, retryable=retryable)
        elif isinstance(res, dict) and "error" in res:
            _mark_failed(
                factory, tid, iid, res.get("error_code") or res["error"],
                retryable=bool(res.get("retryable", True)),
            )
        else:
            _apply_qa_progress(factory, tid, res)
    for tid in {t for t, _i, _m in qa_jobs}:
        _mark_task_completed(factory, tid)
    return len(qa_jobs)


def finalize_stale_tasks(db) -> int:
    """任务与窗口状态校准（worker 空闲时执行）：

    1. 已完成/失败/取消任务的残留 pending/running 窗口 → failed（防"已完成的上一个任务"被重跑）；
    2. status=running 且全部窗口已到终态的任务 → completed（补齐崩溃前未落的状态）。
    """
    fixed = 0
    # 非活动任务的残留窗口一律终止，不再领取执行
    rows = list(
        db.scalars(
            select(TaskItem)
            .join(Task, TaskItem.task_id == Task.id)
            .where(
                TaskItem.status.in_(["pending", "running"]),
                Task.status.notin_(["running", "queued", "paused"]),
            )
        )
    )
    for it in rows:
        it.status = "failed"
        it.error_code = "stale_task"
        it.finished_at = datetime.now(timezone.utc)
        fixed += 1

    # 窗口全终态的 running 任务补齐 completed 状态
    for tid in db.scalars(select(Task.id).where(Task.status == "running")).all():
        has_items = db.scalar(
            select(TaskItem.id).where(TaskItem.task_id == tid).limit(1)
        )
        if has_items is None:
            continue  # 线程型任务（express/parse 等）由 runner 管理状态
        pending = db.scalar(
            select(func.count())
            .select_from(TaskItem)
            .where(
                TaskItem.task_id == tid,
                TaskItem.status.in_(["pending", "running"]),
            )
        )
        if pending == 0:
            from app.services.quality_service import auto_approve_project, project_quality_report

            failed = db.scalar(
                select(func.count())
                .select_from(TaskItem)
                .where(TaskItem.task_id == tid, TaskItem.status == "failed")
            )
            t = db.get(Task, tid)
            settle_task_timer(t)
            if failed:
                t.status = "failed"
                t.error_code = "items_failed"
                t.error_message = f"有 {failed} 个执行节点失败，请重试。"
            else:
                quality = project_quality_report(db, t.project_id)
                progress = json.loads(t.progress_json or "{}")
                progress["quality"] = quality
                t.progress_json = json.dumps(progress, ensure_ascii=False)
                if quality.get("gate") == "passed":
                    t.progress_json = json.dumps(
                        {**progress, "approve": auto_approve_project(db, t.project_id)},
                        ensure_ascii=False,
                    )
                    t.status = "completed"
                    t.error_message = None
                else:
                    t.status = "failed"
                    t.error_code = "quality_gate"
                    t.error_message = "自动质检未通过：" + str(
                        quality.get("reason") or "存在未解决问题"
                    )
            fixed += 1
    db.commit()
    return fixed


async def worker_loop(stop_event: asyncio.Event | None = None) -> None:
    """常驻循环：并发 Draft 批次 + 并发 Polish 批次。"""
    while stop_event is None or not stop_event.is_set():
        try:
            ran = await run_once()
        except Exception:  # noqa: BLE001 Worker 不因单个错误退出
            ran = 0
        if ran == 0:
            # 空闲时校准任务与窗口状态：杜绝已完成任务的残留窗口被重跑
            try:
                with dbmod.SessionLocal() as db:
                    finalize_stale_tasks(db)
            except Exception:  # noqa: BLE001 校准失败不影响主循环
                pass
            await asyncio.sleep(POLL_SECONDS)
