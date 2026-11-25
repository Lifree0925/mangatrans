"""LLM 调用计量：通过 contextvars 关联任务，Provider 成功/失败后写入 SQLite。

计量失败绝不能影响翻译主流程，因此所有数据库写入均做兜底忽略。
"""

from contextlib import contextmanager
from contextvars import ContextVar
import json
from typing import Any

_TASK_ID: ContextVar[str | None] = ContextVar("llm_task_id", default=None)
_PROJECT_ID: ContextVar[str | None] = ContextVar("llm_project_id", default=None)


def current_usage_task_id() -> str | None:
    """当前计量上下文中的任务 ID。"""
    return _TASK_ID.get()


@contextmanager
def usage_context(task_id: str | None, project_id: str | None):
    """在当前协程/线程内绑定任务与项目。"""
    t = _TASK_ID.set(task_id)
    p = _PROJECT_ID.set(project_id)
    try:
        yield
    finally:
        _PROJECT_ID.reset(p)
        _TASK_ID.reset(t)


@contextmanager
def usage_context_for_task(task_id: str):
    """从数据库读取任务所属项目，供没有现成 project_id 的 Worker 使用。"""
    project_id = None
    try:
        import app.core.database as dbmod
        from app.models.task import Task

        with dbmod.SessionLocal() as db:
            task = db.get(Task, task_id)
            project_id = task.project_id if task is not None else None
    except Exception:  # noqa: BLE001 计量上下文失败不影响任务
        project_id = None
    with usage_context(task_id, project_id):
        yield


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_values(usage: dict) -> dict:
    prompt = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
    reasoning = _int(
        usage.get("reasoning_tokens")
        or (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    )
    cached = _int(
        usage.get("cached_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        # DeepSeek / 兼容网关的字段名
        or usage.get("prompt_cache_hit_tokens")
        or usage.get("cache_read_input_tokens")
        or usage.get("cached_prompt_tokens")
    )
    total = _int(usage.get("total_tokens")) or prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "cached_tokens": cached,
        "total_tokens": total,
    }


def record_usage(
    *,
    stage: str,
    model_name: str,
    provider_id: str = "",
    usage: dict | None = None,
    cache_hit: bool = False,
    latency_ms: float = 0,
    finish_reason: str | None = None,
    error_code: str | None = None,
    success: bool = True,
    degradations: list[dict] | None = None,
) -> None:
    """写入一条阶段级模型调用记录。失败静默，绝不干扰业务。"""
    try:
        import app.core.database as dbmod
        from app.models.usage import LLMCall

        values = _usage_values(usage or {})
        with dbmod.SessionLocal() as db:
            db.add(
                LLMCall(
                    task_id=_TASK_ID.get(),
                    project_id=_PROJECT_ID.get(),
                    stage=(stage or "llm")[:32],
                    model_name=(model_name or "")[:200],
                    provider_id=(provider_id or "")[:100],
                    **values,
                    cache_hit=bool(cache_hit),
                    success=bool(success),
                    latency_ms=float(latency_ms or 0),
                    finish_reason=(finish_reason or None),
                    error_code=(error_code or None),
                    degradations_json=json.dumps(degradations or [], ensure_ascii=False),
                )
            )
            db.commit()
    except Exception:  # noqa: BLE001 计量失败不得影响翻译
        return
