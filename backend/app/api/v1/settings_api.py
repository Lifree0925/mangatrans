"""模型设置与任务中心 API。"""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import credentials
from app.core.database import get_db
from app.models.task import Task
from app.models.translation import ProviderConfig
from app.services.provider_service import set_default_from_db, test_provider

router = APIRouter(tags=["settings"])


@router.get("/settings/providers")
def list_providers(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(ProviderConfig).order_by(ProviderConfig.created_at))
    return [
        {
            "id": r.id,
            "display_name": r.display_name,
            "base_url": r.base_url,
            "model_name": r.model_name,
            "credential_ref": r.credential_ref,
            "has_credential": bool(credentials.get_credential(r.credential_ref)),
            "is_default": r.is_default,
            "config_version": r.config_version,
        }
        for r in rows
    ]


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
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    if payload.get("api_key"):
        credentials.save_credential(cfg.credential_ref, payload["api_key"])
        set_default_from_db(db)
    return {"id": cfg.id, "config_version": cfg.config_version, "has_credential": bool(payload.get("api_key"))}


@router.put("/settings/providers/{pid}/credential")
def put_credential(pid: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="provider not found")
    credentials.save_credential(cfg.credential_ref, payload["api_key"])
    return {"ok": True}


@router.delete("/settings/providers/{pid}/credential")
def del_credential(pid: str, db: Session = Depends(get_db)) -> dict:
    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="provider not found")
    credentials.delete_credential(cfg.credential_ref)
    return {"ok": True}


@router.post("/settings/providers/{pid}/test")
async def test_provider_conn(pid: str, db: Session = Depends(get_db)) -> dict:
    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return await test_provider(cfg)


@router.patch("/settings/providers/{pid}")
def patch_provider(pid: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    cfg = db.get(ProviderConfig, pid)
    if cfg is None:
        raise HTTPException(status_code=404, detail="provider not found")
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


@router.get("/tasks/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "progress": json.loads(task.progress_json or "{}"),
        "error_message": task.error_message,
    }


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status == "running":
        task.status = "paused"
        db.commit()
    return {"id": task.id, "status": task.status}


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status in ("paused", "failed"):
        task.status = "running"
        db.commit()
    return {"id": task.id, "status": task.status}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    from sqlalchemy import update

    db.execute(
        update(TaskItem)
        .where(TaskItem.task_id == task_id, TaskItem.status == "failed")
        .values(status="pending")
    )
    task.status = "running"
    db.commit()
    return {"id": task.id, "status": task.status}


@router.get("/projects/{project_id}/tasks")
def list_project_tasks(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    tasks = db.scalars(
        select(Task).where(Task.project_id == project_id).order_by(Task.created_at.desc()).limit(50)
    )
    return [
        {
            "id": t.id,
            "task_type": t.task_type,
            "status": t.status,
            "progress": json.loads(t.progress_json or "{}"),
            "error_message": t.error_message,
        }
        for t in tasks
    ]
