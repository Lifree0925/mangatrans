"""模型设置与任务中心 API。"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import credentials
from app.core.database import get_db
from app.models.task import Task, TaskItem
from app.models.translation import ProviderConfig
from app.llm.activity import active_calls_snapshot
from app.pipeline.runner import launch as launch_task
from app.pipeline.task_timer import (
    current_task_run_seconds,
    elapsed_task_seconds,
    settle_task_timer,
    start_task_timer,
)
from app.services.provider_service import set_default_from_db, test_provider
from app.services.usage_service import summarize_one, summarize_task_usage

router = APIRouter(tags=["settings"])


def _iso_utc(dt) -> str | None:
    """时间戳序列化：SQLite 存时间时丢失时区标记（库内值为 UTC naive），
    补 Z 后缀供前端按 UTC 正确解析（否则本地时区会多算 8 小时）。"""
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.isoformat() + "Z"
    return dt.isoformat()


def _compact_progress(raw: str) -> dict:
    """任务列表只返回展示所需进度，裁掉 draft_carry 等大体积内部快照。

    详情接口仍返回完整 progress，便于排查；列表每 1.5 秒轮询，必须控制体积。
    """
    try:
        progress = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(progress, dict):
        return {}
    top_carry = progress.pop("draft_carry", None)
    if isinstance(top_carry, dict):
        progress["draft_done"] = max(int(progress.get("draft_done") or 0), len(top_carry))
    steps = progress.get("steps")
    if isinstance(steps, dict):
        translation = steps.get("translation")
        if isinstance(translation, dict):
            carry = translation.pop("draft_carry", None)
            if isinstance(carry, dict):
                translation["draft_done"] = max(
                    int(translation.get("draft_done") or 0), len(carry)
                )
            translation.pop("failed_window_errors", None)
    return progress


def _latest_activity_at(task: Task, usage: dict | None) -> str | None:
    """任务最近一次真实活动：进度写库时间与最近模型调用取较晚者。"""
    values = [task.updated_at, (usage or {}).get("last_call_at")]
    latest: datetime | None = None
    for value in values:
        if value is None:
            continue
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if getattr(value, "tzinfo", None) is None
            else value.astimezone(timezone.utc)
        )
        if latest is None or normalized > latest:
            latest = normalized
    return _iso_utc(latest)


@router.get("/settings/providers")
def list_providers(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(ProviderConfig).order_by(ProviderConfig.created_at))
    def _mask(secret: str | None) -> str | None:
        """Key 掩码：保留首尾 4 位，中间用 * 隐藏。"""
        if not secret:
            return None
        if len(secret) <= 8:
            return "*" * len(secret)
        return secret[:4] + "*" * max(4, len(secret) - 8) + secret[-4:]

    result = []
    for r in rows:
        secret = credentials.get_credential(r.credential_ref)
        result.append(
            {
                "id": r.id,
                "display_name": r.display_name,
                "base_url": r.base_url,
                "model_name": r.model_name,
                "credential_ref": r.credential_ref,
                "has_credential": bool(secret),
                "masked_key": _mask(secret),
                "is_default": r.is_default,
                "is_verified": r.is_verified,
                "last_tested_at": _iso_utc(r.last_tested_at),
                "last_test_error": r.last_test_error,
                "config_version": r.config_version,
            }
        )
    return result


@router.post("/settings/providers", status_code=201)
def create_provider(payload: dict, db: Session = Depends(get_db)) -> dict:
    """创建模型服务。API Key 只写入系统凭据库，不进入数据库。"""
    import uuid

    cfg = ProviderConfig(
        display_name=payload.get("display_name") or payload["model_name"],
        base_url=payload["base_url"],
        model_name=payload["model_name"],
        credential_ref=payload.get("credential_ref") or f"provider-{uuid.uuid4().hex[:12]}",
        api_mode=payload.get("api_mode", "openai-compatible"),
        timeout_seconds=payload.get("timeout_seconds", 120),
        max_retries=payload.get("max_retries", 2),
        is_default=payload.get("is_default", False),
    )
    api_key = payload.get("api_key")
    if payload.get("api_key"):
        credentials.save_credential(cfg.credential_ref, api_key)
    db.add(cfg)
    try:
        db.commit()
    except Exception:
        db.rollback()
        if api_key:
            try:
                credentials.delete_credential(cfg.credential_ref)
            except Exception:  # noqa: BLE001 配置创建失败时尽量清理孤立 Key
                pass
        raise
    db.refresh(cfg)
    if api_key:
        set_default_from_db(db)
    return {"id": cfg.id, "config_version": cfg.config_version, "has_credential": bool(api_key)}


@router.put("/settings/providers/{pid}/credential")
def put_credential(pid: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    credentials.save_credential(cfg.credential_ref, payload["api_key"])
    return {"ok": True}


@router.delete("/settings/providers/{pid}/credential")
def del_credential(pid: str, db: Session = Depends(get_db)) -> dict:
    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    credentials.delete_credential(cfg.credential_ref)
    return {"ok": True}


@router.post("/settings/providers/{pid}/test")
async def test_provider_conn(pid: str, db: Session = Depends(get_db)) -> dict:
    """连通性测试并持久化结果：只有通过测试的模型可用于翻译。"""
    from datetime import datetime, timezone

    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    result = await test_provider(cfg)
    cfg.is_verified = bool(result.get("ok"))
    cfg.last_tested_at = datetime.now(timezone.utc)
    cfg.last_test_error = None if result.get("ok") else str(result.get("detail", ""))[:500]
    db.commit()
    set_default_from_db(db)
    return result


@router.patch("/settings/providers/{pid}")
def patch_provider(pid: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    for field in ("display_name", "base_url", "model_name", "timeout_seconds", "max_retries"):
        if field in payload:
            setattr(cfg, field, payload[field])
    # API Key 永远不写数据库，只更新系统凭据库
    if payload.get("api_key"):
        credentials.save_credential(cfg.credential_ref, payload["api_key"])
    if "is_default" in payload and payload["is_default"]:
        for other in db.scalars(select(ProviderConfig).where(ProviderConfig.id != pid)):
            other.is_default = False
        cfg.is_default = True
    cfg.config_version += 1
    db.commit()
    set_default_from_db(db)
    return {"id": cfg.id, "config_version": cfg.config_version}


def _reset_task_items_for_resume(
    db: Session, task_id: str, *, reset_running: bool
) -> bool:
    """恢复批量任务：只把失败/中断节点放回 pending，成功节点保持原状。"""
    has_items = db.scalar(
        select(TaskItem.id).where(TaskItem.task_id == task_id).limit(1)
    )
    if has_items is None:
        return False
    statuses = ["failed"]
    if reset_running:
        statuses.append("running")
    db.execute(
        update(TaskItem)
        .where(
            TaskItem.task_id == task_id,
            TaskItem.status.in_(statuses),
        )
        .values(
            status="pending",
            started_at=None,
            finished_at=None,
            error_code=None,
        )
    )
    return True


def _reset_quality_steps_for_retry(task: Task, error_code: str | None = None) -> None:
    """质量门禁失败后重试时，清掉 QA 完成标记，让自动质检真正重跑。"""
    error_code = error_code if error_code is not None else task.error_code
    if error_code not in ("quality_gate", "qa_incomplete"):
        return
    try:
        progress = json.loads(task.progress_json or "{}")
    except json.JSONDecodeError:
        return
    steps = progress.get("steps")
    if isinstance(steps, dict):
        steps.pop("qa", None)
        steps.pop("approve", None)
        progress["steps"] = steps
        task.progress_json = json.dumps(progress, ensure_ascii=False)


def _claim_task_for_running(
    db: Session, task: Task, *, previous_updated_at, previous_error_code,
    reset_items: bool = False, reset_running: bool = False,
):
    """原子抢占任务状态：同一任务/同一项目并发重试时只有一次能成功。"""
    result = db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == task.status)
        .values(status="running", error_message=None, error_code=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        db.refresh(task)
        return False
    db.refresh(task)
    if reset_items:
        _reset_task_items_for_resume(
            db, task.id, reset_running=reset_running
        )
    settle_task_timer(task, until=previous_updated_at)
    _reset_quality_steps_for_retry(task, previous_error_code)
    task.status = "running"
    task.error_message = None
    task.error_code = None
    start_task_timer(task)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="该项目已有任务正在执行，请等待完成后再继续",
        ) from exc
    return True

@router.get("/tasks/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    usage = summarize_one(db, task.id)
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "progress": json.loads(task.progress_json or "{}"),
        "error_message": task.error_message,
        "error_code": task.error_code,
        "created_at": _iso_utc(task.created_at),
        "started_at": _iso_utc(task.started_at),
        "active_seconds": round(float(task.active_seconds or 0), 1),
        "elapsed_seconds": elapsed_task_seconds(task),
        "last_activity_at": _latest_activity_at(task, usage),
        "live": active_calls_snapshot([task.id]).get(task.id, {"active_calls": 0}),
        "model": json.loads(task.config_snapshot_json or "{}").get("model", ""),
        "usage": usage,
    }


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 条件更新：任务若已在这期间完成/失败，不能被迟到的暂停请求覆盖。
    db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "running")
        .values(status="paused")
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(task)
    return {"id": task.id, "status": task.status}


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 同项目并发防护：已有其他运行/排队任务时拒绝继续，防止双任务写冲突
    other = db.scalar(
        select(Task.id).where(
            Task.project_id == task.project_id,
            Task.status.in_(["running", "queued", "paused"]),
            Task.id != task.id,
        ).limit(1)
    )
    if other:
        raise HTTPException(status_code=409, detail="该项目已有任务正在执行，请等待完成后再继续")
    has_items = db.scalar(
        select(TaskItem.id).where(TaskItem.task_id == task_id).limit(1)
    )
    if has_items is None:
        # 线程型任务（Express/试译/知识/场景卡）：必须真正重新调度执行
        if task.status == "running" and task.started_at is not None:
            # 仍在执行（30 分钟内）：无需重复调度
            if current_task_run_seconds(task) < 30 * 60:
                return {"id": task.id, "status": task.status}
        if task.status == "completed":
            return {"id": task.id, "status": task.status}
    else:
        if task.status not in ("paused", "failed"):
            return {"id": task.id, "status": task.status}
    previous_status = task.status
    previous_updated_at = task.updated_at
    previous_error_code = task.error_code
    claimed = _claim_task_for_running(
        db,
        task,
        previous_updated_at=previous_updated_at,
        previous_error_code=previous_error_code,
        reset_items=has_items,
        reset_running=previous_status == "failed",
    )
    if not claimed:
        return {"id": task.id, "status": task.status}
    if has_items is None:
        # 仅改状态不会重新执行，必须重新调度
        launch_task(task_id)
    return {"id": task.id, "status": task.status}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status == "completed":
        return {"id": task.id, "status": task.status}
    # 幂等：连续点击/双击重试时，首次调用已把任务置为 running 并启动线程，
    # 后续请求必须直接返回，不能再次 launch 同一任务。
    if task.status in ("running", "queued"):
        return {"id": task.id, "status": task.status}
    # 同项目并发防护：已有运行中任务时禁止重试，防止两个任务互相卡住
    other = db.scalar(
        select(Task.id).where(
            Task.project_id == task.project_id,
            Task.status.in_(["running", "queued", "paused"]),
            Task.id != task.id,
        ).limit(1)
    )
    if other:
        raise HTTPException(status_code=409, detail="该项目已有任务正在执行，请等待完成后再重试")
    # 批量任务：失败节点回 pending，成功节点（Draft/Polish/QA）绝不重跑。
    # 线程型任务没有 TaskItem，才需要重新 launch。
    has_items = db.scalar(
        select(TaskItem.id).where(TaskItem.task_id == task_id).limit(1)
    ) is not None
    previous_status = task.status
    previous_updated_at = task.updated_at
    previous_error_code = task.error_code
    claimed = _claim_task_for_running(
        db,
        task,
        previous_updated_at=previous_updated_at,
        previous_error_code=previous_error_code,
        reset_items=has_items,
        reset_running=previous_status == "failed",
    )
    if not claimed:
        return {"id": task.id, "status": task.status}
    if not has_items:
        # 线程型任务：重新调度执行
        launch_task(task_id)
    return {"id": task.id, "status": task.status}


@router.post("/tasks/{task_id}/archive")
def archive_task(task_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """归档/取消归档任务：归档后任务中心隐藏，历史记录查看。"""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail="任务仍在执行，完成后才能归档")
    task.archived = bool(payload.get("archived", True))
    db.commit()
    return {"id": task.id, "archived": task.archived}


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: str, db: Session = Depends(get_db)) -> None:
    """硬删除任务记录（仅限失败/取消/已完成；运行中拒绝）。"""
    from sqlalchemy import delete as sa_delete

    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in ("running", "queued"):
        raise HTTPException(status_code=409, detail="任务正在执行，无法删除")
    from app.models.qa import WindowQuality
    from app.models.usage import LLMCall

    db.execute(sa_delete(LLMCall).where(LLMCall.task_id == task_id))
    db.execute(sa_delete(WindowQuality).where(WindowQuality.task_id == task_id))
    db.execute(sa_delete(TaskItem).where(TaskItem.task_id == task_id))
    db.delete(task)
    db.commit()


@router.get("/tasks")
def list_all_tasks(db: Session = Depends(get_db), archived: bool = False) -> list[dict]:
    """任务中心：archived=false 返回活动任务；archived=true 返回历史记录。"""
    from app.models.project import Project

    # 台词抽取/对齐已并入 Express 管线，历史独立任务不再展示（数据保留可审计）
    tasks = db.scalars(
        select(Task)
        .where(
            Task.archived.is_(archived),
            Task.task_type.notin_(("dialogue", "dialogue_align", "parse")),
        )
        .order_by(Task.created_at.desc())
        .limit(200)
    ).all()
    names = {
        p.id: p.name
        for p in db.scalars(select(Project).where(Project.id.in_({t.project_id for t in tasks})))
    }
    task_ids = [t.id for t in tasks]
    usage = summarize_task_usage(db, task_ids)
    live = active_calls_snapshot(task_ids)
    return [
        {
            "id": t.id,
            "task_type": t.task_type,
            "status": t.status,
            "progress": _compact_progress(t.progress_json),
            "error_message": t.error_message,
            "error_code": t.error_code,
            "created_at": _iso_utc(t.created_at),
            "started_at": _iso_utc(t.started_at),
            "active_seconds": round(float(t.active_seconds or 0), 1),
            "elapsed_seconds": elapsed_task_seconds(t),
            "last_activity_at": _latest_activity_at(t, usage.get(t.id)),
            "live": live.get(t.id, {"active_calls": 0}),
            "project_id": t.project_id,
            "project_name": names.get(t.project_id, "已删除项目"),
            "model": json.loads(t.config_snapshot_json or "{}").get("model", ""),
            "archived": t.archived,
            "usage": usage.get(t.id, {}),
        }
        for t in tasks
    ]


@router.get("/projects/{project_id}/tasks")
def list_project_tasks(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    from app.models.project import Project

    tasks = db.scalars(
        select(Task).where(Task.project_id == project_id).order_by(Task.created_at.desc()).limit(50)
    )
    task_ids = [t.id for t in tasks]
    usage = summarize_task_usage(db, task_ids)
    live = active_calls_snapshot(task_ids)
    return [
        {
            "id": t.id,
            "task_type": t.task_type,
            "status": t.status,
            "progress": _compact_progress(t.progress_json),
            "error_message": t.error_message,
            "error_code": t.error_code,
            "created_at": _iso_utc(t.created_at),
            "started_at": _iso_utc(t.started_at),
            "active_seconds": round(float(t.active_seconds or 0), 1),
            "elapsed_seconds": elapsed_task_seconds(t),
            "last_activity_at": _latest_activity_at(t, usage.get(t.id)),
            "live": live.get(t.id, {"active_calls": 0}),
            "project_name": db.get(Project, t.project_id).name if db.get(Project, t.project_id) else "已删除项目",
            "model": json.loads(t.config_snapshot_json or "{}").get("model", ""),
            "usage": usage.get(t.id, {}),
        }
        for t in tasks
    ]


@router.delete("/settings/providers/{pid}", status_code=204)
def delete_provider(pid: str, db: Session = Depends(get_db)) -> None:
    """删除模型服务配置及对应系统凭据。"""
    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    credentials.delete_credential(cfg.credential_ref)
    was_default = cfg.is_default
    db.delete(cfg)
    db.commit()
    if was_default:
        fallback = db.scalar(select(ProviderConfig).limit(1))
        if fallback is not None:
            fallback.is_default = True
            db.commit()
        set_default_from_db(db)
