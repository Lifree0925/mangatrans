"""任务实际运行计时：暂停、应用关闭和崩溃停机时间不计入。"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.task import Task


def _utc(value: datetime) -> datetime:
    """SQLite 读出的时间通常没有时区标记，统一按 UTC 处理。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def start_task_timer(task: Task, now: datetime | None = None) -> None:
    """开始一段运行计时；已有起点时不重置，避免并发领取窗口重复计时。"""
    if task.started_at is None:
        task.started_at = now or datetime.now(timezone.utc)


def settle_task_timer(
    task: Task, now: datetime | None = None, until: datetime | None = None
) -> None:
    """结算当前运行段并清空起点；until 用于把结束点截到最后一次持久化时间。"""
    if task.started_at is None:
        return
    end = _utc(now or datetime.now(timezone.utc))
    if until is not None:
        end = min(end, _utc(until))
    started = _utc(task.started_at)
    seconds = max(0.0, (end - started).total_seconds())
    task.active_seconds = float(task.active_seconds or 0.0) + seconds
    task.started_at = None


def current_task_run_seconds(task: Task, now: datetime | None = None) -> float:
    """当前运行段已持续秒数；没有运行段时返回 0。"""
    if task.started_at is None:
        return 0.0
    started = _utc(task.started_at)
    end = _utc(now or datetime.now(timezone.utc))
    return max(0.0, (end - started).total_seconds())


def elapsed_task_seconds(task: Task, now: datetime | None = None) -> float:
    """累计实际运行秒数（已完成段 + 当前运行段）。"""
    return round(
        float(task.active_seconds or 0.0) + current_task_run_seconds(task, now=now), 1
    )


def settle_task_timer_for_recovery(task: Task) -> None:
    """进程重启恢复：只结算到最后一次任务写库时间，排除停机时段。"""
    settle_task_timer(task, until=task.updated_at)


def settle_active_task_timers(db: Session, now: datetime | None = None) -> int:
    """应用退出时结算所有未完成运行段。"""
    tasks = list(db.scalars(select(Task).where(Task.started_at.is_not(None))))
    for task in tasks:
        if task.status == "paused":
            # 暂停时刻之后不再计时；updated_at 在 pause 接口提交时刷新。
            settle_task_timer(task, until=task.updated_at)
        else:
            settle_task_timer(task, now=now)
    if tasks:
        db.commit()
    return len(tasks)
