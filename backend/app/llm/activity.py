"""模型请求活动登记：供任务中心实时展示「正在调用」和最长等待时间。

登记表仅在当前后端进程内存中，适合本地单 Worker 桌面应用；进程重启后由
任务状态与用量账本恢复。所有操作都加锁，兼容线程型任务与并发窗口。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from app.llm.usage import current_usage_task_id


@dataclass(frozen=True)
class ActiveCall:
    task_id: str
    stage: str
    model: str
    started_monotonic: float


_LOCK = threading.Lock()
_ACTIVE: dict[str, ActiveCall] = {}


def begin_active_call(stage: str, model: str) -> str | None:
    """登记一次真实模型调用；没有任务上下文时返回 None。"""
    task_id = current_usage_task_id()
    if not task_id:
        return None
    call_id = uuid.uuid4().hex
    with _LOCK:
        _ACTIVE[call_id] = ActiveCall(
            task_id=task_id,
            stage=(stage or "llm")[:32],
            model=(model or "")[:200],
            started_monotonic=time.monotonic(),
        )
    return call_id


def end_active_call(call_id: str | None) -> None:
    if not call_id:
        return
    with _LOCK:
        _ACTIVE.pop(call_id, None)


def active_calls_snapshot(task_ids: list[str]) -> dict[str, dict]:
    """返回任务维度的在途请求数、最长等待秒数与阶段/模型列表。"""
    wanted = set(task_ids)
    grouped: dict[str, list[ActiveCall]] = {tid: [] for tid in task_ids}
    now = time.monotonic()
    with _LOCK:
        for call in _ACTIVE.values():
            if call.task_id in wanted:
                grouped[call.task_id].append(call)
    result: dict[str, dict] = {}
    for tid, calls in grouped.items():
        if not calls:
            result[tid] = {
                "active_calls": 0,
                "oldest_seconds": None,
                "stages": [],
                "models": [],
            }
            continue
        result[tid] = {
            "active_calls": len(calls),
            "oldest_seconds": round(
                max(0.0, now - min(c.started_monotonic for c in calls)), 1
            ),
            "stages": sorted({c.stage for c in calls}),
            "models": sorted({c.model for c in calls if c.model}),
        }
    return result
