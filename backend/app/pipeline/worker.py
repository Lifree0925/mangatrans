"""持久 Worker：Draft 阶段并发（6 路），Polish 阶段并发（carry-over 前置）。

- carry-over 由 Draft 阶段顺带生成并写入任务进度（调度层顺序写），
  Polish 消费前一窗口的 Draft carry，因此窗口间无执行依赖，可并发。
- started_at 超时回收：崩溃/僵死后自动回 pending，断点续跑。
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app.core.database as dbmod
from app.models.task import Task, TaskItem
from app.services.translation_batch import process_window_draft_item, process_window_polish_item

LEASE_TIMEOUT_MINUTES = 30
POLL_SECONDS = 1.0
WINDOW_TIMEOUT_SECONDS = 600  # 单窗口硬超时：思考模式慢调用兜底，防 gather 僵死
DRAFT_CONCURRENCY = 10
POLISH_CONCURRENCY = 10


def recover_stale_items(db) -> int:
    """启动时回收：running 且 started_at 超时且无成功结果 → pending。"""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LEASE_TIMEOUT_MINUTES)
    rows = list(
        db.scalars(
            select(TaskItem).where(
                TaskItem.status == "running",
                TaskItem.started_at.is_not(None),
                TaskItem.started_at < cutoff,
            )
        )
    )
    for item in rows:
        item.status = "pending"
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
    """原子领取最多 limit 个 draft 待办（并发执行）。"""
    rows = list(
        db.scalars(
            select(TaskItem)
            .where(TaskItem.status == "pending", TaskItem.phase == "draft")
            .order_by(TaskItem.window_id)
            .limit(limit)
        )
    )
    claimed: list[TaskItem] = []
    for item in rows:
        if not _task_usable(db, item.task_id):
            continue
        item.status = "running"
        item.started_at = datetime.now(timezone.utc)
        item.attempts += 1
        claimed.append(item)
    db.commit()
    return claimed


def claim_polish_batch(db, limit: int = POLISH_CONCURRENCY) -> list[TaskItem]:
    """原子领取最多 limit 个 polish 待办（并发执行；carry 来自前一窗口 Draft，无串行依赖）。"""
    rows = list(
        db.scalars(
            select(TaskItem)
            .where(TaskItem.status == "pending", TaskItem.phase == "polish")
            .order_by(TaskItem.window_id)
            .limit(limit)
        )
    )
    claimed: list[TaskItem] = []
    for item in rows:
        if not _task_usable(db, item.task_id):
            continue
        task = db.get(Task, item.task_id)
        if task is not None and task.status == "queued":
            task.status = "running"
        item.status = "running"
        item.started_at = datetime.now(timezone.utc)
        item.attempts += 1
        claimed.append(item)
    db.commit()
    return claimed


def _mark_failed(factory, task_id: str, item_id: str, code: str) -> None:
    """失败处理：attempts < 3 时自动回 pending 重试（失败节点续跑），
    attempts 达上限才标记 failed 并累计任务进度中的 failed 计数。"""
    with factory() as db:
        item = db.get(TaskItem, item_id)
        if item is None:
            return
        item.error_code = (code or type(Exception).__name__)[:32]
        if item.attempts < 3:
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
        done = int(progress.get("done", 0)) + 1
        progress["done"] = done
        total = int(progress.get("total", 0)) or db.scalar(
            select(func.count()).select_from(TaskItem).where(TaskItem.task_id == task_id)
        ) or done
        progress["total"] = total
        progress["phase"] = "polish"
        task.progress_json = json.dumps(progress, ensure_ascii=False)
        task.last_checkpoint = res.get("window_id")
        db.commit()


def _mark_task_completed(factory, task_id: str) -> None:
    """全部窗口 polished 成功且无 draft 遗留 → 任务 completed。"""
    with factory() as db:
        remaining = db.scalar(
            select(func.count())
            .select_from(TaskItem)
            .where(
                TaskItem.task_id == task_id,
                TaskItem.status.in_(["pending", "running"]),
            )
        )
        if remaining == 0:
            task = db.get(Task, task_id)
            task.status = "completed"
            db.commit()


async def run_once(db_factory=None) -> int:
    """一批 Draft（并发）或一个 Polish（串行），返回执行数。"""
    factory = db_factory or (lambda: dbmod.SessionLocal())

    with factory() as db:
        recover_stale_items(db)
        drafts = claim_draft_batch(db)
        jobs = [(it.task_id, it.id, _model_of(db, it.task_id)) for it in drafts]
    if drafts:
        results = await asyncio.gather(
            *[
                asyncio.wait_for(
                    process_window_draft_item(factory, tid, iid, model),
                    timeout=WINDOW_TIMEOUT_SECONDS,
                )
                for tid, iid, model in jobs
            ],
            return_exceptions=True,
        )
        for (tid, iid, _model), res in zip(jobs, results):
            if isinstance(res, BaseException):
                _mark_failed(factory, tid, iid, type(res).__name__)
            elif isinstance(res, dict) and "error" in res:
                _mark_failed(factory, tid, iid, res["error"])
        for tid in {tid for tid, _i, _m in jobs}:
            _apply_draft_carry(factory, tid, results)
        return len(drafts)

    with factory() as db:
        items = claim_polish_batch(db)
        jobs = [(it.task_id, it.id, _model_of(db, it.task_id)) for it in items]
    if not jobs:
        return 0

    results = await asyncio.gather(
        *[
            asyncio.wait_for(
                process_window_polish_item(factory, tid, iid, model),
                timeout=WINDOW_TIMEOUT_SECONDS,
            )
            for tid, iid, model in jobs
        ],
        return_exceptions=True,
    )
    touched_tasks: set[str] = set()
    for (tid, iid, _model), res in zip(jobs, results):
        touched_tasks.add(tid)
        if isinstance(res, BaseException):
            _mark_failed(factory, tid, iid, type(res).__name__)
        elif isinstance(res, dict) and "error" in res:
            _mark_failed(factory, tid, iid, res["error"])
        else:
            _apply_polish_progress(factory, tid, res)
    for tid in touched_tasks:
        _mark_task_completed(factory, tid)
    return len(jobs)


async def worker_loop(stop_event: asyncio.Event | None = None) -> None:
    """常驻循环：并发 Draft 批次 + 并发 Polish 批次。"""
    while stop_event is None or not stop_event.is_set():
        try:
            ran = await run_once()
        except Exception:  # noqa: BLE001 Worker 不因单个错误退出
            ran = 0
        if ran == 0:
            await asyncio.sleep(POLL_SECONDS)
