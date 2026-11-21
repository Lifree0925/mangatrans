"""持久 Worker：Draft 阶段并发（6 路），Polish 阶段串行传递 carry-over。

- 两阶段调度：先并发领取 phase='draft' 的 pending 项（最多 DRAFT_CONCURRENCY 个），
  全部转 phase='polish'；再按 window_id 顺序串行执行 polish。
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
DRAFT_CONCURRENCY = 6


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


def claim_polish_next(db) -> TaskItem | None:
    """原子领取一个 polish 待办（串行执行，保持窗口顺序）。"""
    item = db.scalar(
        select(TaskItem)
        .where(TaskItem.status == "pending", TaskItem.phase == "polish")
        .order_by(TaskItem.window_id)
        .limit(1)
    )
    if item is None:
        return None
    if not _task_usable(db, item.task_id):
        return None
    item.status = "running"
    item.started_at = datetime.now(timezone.utc)
    item.attempts += 1
    db.commit()
    return item


def _mark_failed(factory, task_id: str, item_id: str, code: str) -> None:
    with factory() as db:
        item = db.get(TaskItem, item_id)
        item.status = "failed"
        item.error_code = (code or type(Exception).__name__)[:32]
        item.finished_at = datetime.now(timezone.utc)
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
                process_window_draft_item(factory, tid, iid, model)
                for tid, iid, model in jobs
            ],
            return_exceptions=True,
        )
        for (tid, iid, _model), res in zip(jobs, results):
            if isinstance(res, BaseException):
                _mark_failed(factory, tid, iid, type(res).__name__)
            elif isinstance(res, dict) and "error" in res:
                _mark_failed(factory, tid, iid, res["error"])
        return len(drafts)

    with factory() as db:
        item = claim_polish_next(db)
        if item is None:
            return 0
        tid, iid = item.task_id, item.id
        model = _model_of(db, tid)
        item.status = "running"
        db.commit()

    try:
        result = await process_window_polish_item(factory, tid, iid, model)
    except Exception as exc:  # noqa: BLE001
        _mark_failed(factory, tid, iid, type(exc).__name__)
        return 1

    if "error" in result:
        _mark_failed(factory, tid, iid, result["error"])
    _mark_task_completed(factory, tid)
    return 1


async def worker_loop(stop_event: asyncio.Event | None = None) -> None:
    """常驻循环：并发 Draft 批次 + 串行 Polish。"""
    while stop_event is None or not stop_event.is_set():
        try:
            ran = await run_once()
        except Exception:  # noqa: BLE001 Worker 不因单个错误退出
            ran = 0
        if ran == 0:
            await asyncio.sleep(POLL_SECONDS)
