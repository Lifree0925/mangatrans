"""知识、场景卡与提取任务 API。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.document import SourceDocument
from app.models.knowledge import KnowledgeItem, SceneContextCard
from app.models.project import Project
from app.pipeline.runner import create_task, launch
from app.schemas.knowledge import KnowledgeOut, SceneCardOut

router = APIRouter(prefix="/projects/{project_id}", tags=["knowledge"])


def _project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return p


def _active_doc(db: Session, project: Project) -> SourceDocument:
    if project.active_source_document_id is None:
        raise HTTPException(status_code=404, detail="尚未解析原文")
    doc = db.get(SourceDocument, project.active_source_document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


@router.post("/knowledge/extract")
def extract_knowledge(
    project_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    """创建知识提取任务（异步执行）。payload: {mode: express|guided, model?}"""
    project = _project(db, project_id)
    doc = _active_doc(db, project)
    task = create_task(
        db,
        project_id,
        "knowledge",
        {
            "source_document_id": doc.id,
            "mode": payload.get("mode", "guided"),
            "model": payload.get("model", "fake-model"),
        },
    )
    launch(task.id)
    return {"task_id": task.id}


@router.post("/scene-cards/generate")
def generate_scene_cards(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    project = _project(db, project_id)
    doc = _active_doc(db, project)
    task = create_task(
        db,
        project_id,
        "scene_cards",
        {"source_document_id": doc.id, "model": payload.get("model", "fake-model")},
    )
    launch(task.id)
    return {"task_id": task.id}


@router.get("/knowledge", response_model=list[KnowledgeOut])
def list_knowledge(project_id: str, db: Session = Depends(get_db)) -> list[KnowledgeItem]:
    return list(
        db.scalars(
            select(KnowledgeItem)
            .where(KnowledgeItem.project_id == project_id)
            .order_by(KnowledgeItem.updated_at.desc())
        )
    )


@router.patch("/knowledge/{item_id}")
def patch_knowledge(
    project_id: str, item_id: str, payload: dict, db: Session = Depends(get_db)
) -> dict:
    item = db.get(KnowledgeItem, item_id)
    if item is None or item.project_id != project_id:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    if "status" in payload and payload["status"] in {
        "candidate",
        "approved",
        "auto_approved",
        "rejected",
    }:
        item.status = payload["status"]
        item.version += 1
    if "canonical_key" in payload:
        item.canonical_key = payload["canonical_key"]
        item.version += 1
    stale = 0
    if "status" in payload or "canonical_key" in payload:
        from app.services.translation_batch import mark_stale_for_knowledge

        stale = mark_stale_for_knowledge(db, item.id)
    return {"id": item.id, "status": item.status, "version": item.version, "stale_translations": stale}


@router.get("/scene-cards", response_model=list[SceneCardOut])
def list_scene_cards(project_id: str, db: Session = Depends(get_db)) -> list[SceneContextCard]:
    return list(
        db.scalars(
            select(SceneContextCard)
            .where(SceneContextCard.project_id == project_id)
            .order_by(SceneContextCard.updated_at.desc())
        )
    )
