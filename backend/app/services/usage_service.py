"""任务级 LLM 用量汇总。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.usage import LLMCall


def summarize_task_usage(db: Session, task_ids: list[str]) -> dict[str, dict]:
    """按任务汇总请求数、Token、缓存命中和阶段明细。"""
    if not task_ids:
        return {}
    rows = list(db.scalars(select(LLMCall).where(LLMCall.task_id.in_(task_ids))))
    grouped: dict[str, dict] = {}
    for tid in task_ids:
        grouped[tid] = {
            "requests": 0,
            "failed_requests": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "api_total_tokens": 0,
            "latency_ms": 0,
            "by_stage": {},
            "last_call_at": None,
        }
    for row in rows:
        if row.task_id is None:
            continue
        summary = grouped.setdefault(row.task_id, {
            "requests": 0, "failed_requests": 0, "cache_hits": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "api_total_tokens": 0, "latency_ms": 0, "by_stage": {},
            "last_call_at": None,
        })
        summary["requests"] += 1
        if row.created_at is not None:
            previous = summary.get("last_call_at")
            if previous is None or row.created_at > previous:
                summary["last_call_at"] = row.created_at
        summary["latency_ms"] += row.latency_ms
        if not row.success:
            summary["failed_requests"] += 1
        if row.cache_hit:
            summary["cache_hits"] += 1
        if not row.cache_hit and row.success:
            summary["prompt_tokens"] += row.prompt_tokens
            summary["completion_tokens"] += row.completion_tokens
            summary["reasoning_tokens"] += row.reasoning_tokens
            summary["api_total_tokens"] += row.total_tokens
        stage = summary["by_stage"].setdefault(
            row.stage,
            {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "latency_ms": 0},
        )
        stage["requests"] += 1
        stage["latency_ms"] += row.latency_ms
        if not row.cache_hit and row.success:
            stage["prompt_tokens"] += row.prompt_tokens
            stage["completion_tokens"] += row.completion_tokens
            stage["total_tokens"] += row.total_tokens
    return grouped


def summarize_one(db: Session, task_id: str) -> dict:
    return summarize_task_usage(db, [task_id]).get(task_id, {})
