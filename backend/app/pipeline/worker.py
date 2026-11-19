"""持久 Worker：单 Worker 串行领取 task_items，started_at 超时回收。"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app.core.database as dbmod
from app.models.task import Task, TaskItem
from app.services.translation_batch import process_window_item

LEASE_TIMEOUT_MINUTES = 30
POLL_SECONDS = 1.0


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


def claim_next(db) -> TaskItem | None:
    """原子领取一个 pending task_item。"""
    item = db.scalar(
        select(TaskItem)
        .where(TaskItem.status == "pending")
        .order_by(TaskItem.window_id)
        .limit(1)
    )
    if item is None:
        return None
    # 任务级暂停/取消检查
    task = db.get(Task, item.task_id)
    if task is None or task.status in ("paused", "failed", "cancelled"):
        return None
    item.status = "running"
    item.started_at = datetime.now(timezone.utc)
    item.attempts += 1
    db.commit()
    return item


async def run_once(db_factory=None) -> int:
    """执行一个 pending 项，返回执行数（0 表示无待办）。"""
    factory = db_factory or (lambda: dbmod.SessionLocal())
    with factory() as db:
        recover_stale_items(db)
        item = claim_next(db)
        if item is None:
            return 0
        task_id = item.task_id
        item_id = item.id
        payload = json.loads(
            db.get(Task, task_id).config_snapshot_json or "{}"
        )
        model = payload.get("model", "fake-model")

    try:
        result = await process_window_item(factory, task_id, item_id, model)
    except Exception as exc:  # noqa: BLE001
        with factory() as db:
            item = db.get(TaskItem, item_id)
            item.status = "failed"
            item.error_code = type(exc).__name__[:32]
            item.finished_at = datetime.now(timezone.utc)
            db.commit()
        return 1

    if "error" in result:
        with factory() as db:
            item = db.get(TaskItem, item_id)
            item.status = "failed"
            item.error_code = result["error"][:32]
            db.commit()
        return 1

    # 全部完成则任务 completed
    with factory() as db:
        pending = db.scalar(
            select(func.count())
            .select_from(TaskItem)
            .where(TaskItem.task_id == task_id, TaskItem.status.in_(["pending", "running"]))
        )
        if pending == 0:
            task = db.get(Task, task_id)
            task.status = "completed"
            db.commit()
    return 1


async def worker_loop(stop_event: asyncio.Event | None = None) -> None:
    """常驻循环：串行领取执行。"""
    while stop_event is None or not stop_event.is_set():
        try:
            ran = await run_once()
        except Exception:  # noqa: BLE001 Worker 不因单个错误退出
            ran = 0
        if ran == 0:
            await asyncio.sleep(POLL_SECONDS)
