"""试译、风格基线与译文查询 API。"""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.document import SourceDocument, SourceSegment
from app.models.project import Project
from app.models.task import Task
from app.models.translation import StyleGuide, Translation
from app.pipeline.runner import create_task, launch

router = APIRouter(prefix="/projects/{project_id}", tags=["translation"])


def _project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")
    return p


def _active_doc(db: Session, project: Project) -> SourceDocument:
    if project.active_source_document_id is None:
        raise HTTPException(status_code=404, detail="尚未解析原文")
    return db.get(SourceDocument, project.active_source_document_id)


@router.post("/translation/trial")
def start_trial(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """试译与风格基线任务（自动建立，无需人工确认）。"""
    project = _project(db, project_id)
    doc = _active_doc(db, project)
    from app.services.provider_service import ensure_verified_provider

    ensure_verified_provider(db)
    task = create_task(
        db,
        project_id,
        "translate",
        {
            "kind": "trial",
            "source_document_id": doc.id,
            "model": payload.get("model", "fake-model"),
        },
    )
    launch(task.id)
    return {"task_id": task.id}


@router.get("/style-guide")
def get_style_guide(project_id: str, db: Session = Depends(get_db)) -> dict:
    sg = db.scalar(
        select(StyleGuide)
        .where(StyleGuide.project_id == project_id)
        .order_by(StyleGuide.version.desc())
    )
    if sg is None:
        return {"exists": False}
    return {"exists": True, "id": sg.id, "version": sg.version, "status": sg.status,
            "guide": json.loads(sg.guide_json)}


@router.patch("/style-guide")
def patch_style_guide(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """Guided 模式冻结前修改（冻结后只增不改，此处仅允许 draft）。"""
    sg = db.scalar(
        select(StyleGuide)
        .where(StyleGuide.project_id == project_id)
        .order_by(StyleGuide.version.desc())
    )
    if sg is None:
        raise HTTPException(status_code=404, detail="style guide not found")
    if "guide" in payload:
        sg.guide_json = json.dumps(payload["guide"], ensure_ascii=False)
    db.commit()
    return {"id": sg.id, "version": sg.version, "status": sg.status}


@router.get("/segments/{segment_id}/translations")
def get_translations(segment_id: str, db: Session = Depends(get_db)) -> dict:
    rows = list(
        db.scalars(
            select(Translation)
            .where(Translation.segment_id == segment_id)
            .order_by(Translation.version.desc())
        )
    )
    return {
        "translations": [
            {
                "id": t.id,
                "version": t.version,
                "stage": t.stage,
                "target_text": t.target_text,
                "review_status": t.review_status,
                "freshness_status": t.freshness_status,
                "notes": json.loads(t.notes_json or "[]"),
                "ambiguity": json.loads(t.ambiguity_json or "[]"),
            }
            for t in rows
        ]
    }


@router.post("/translation/start")
def start_batch_translation(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """创建批量翻译任务并展开窗口。payload: {mode?, model?}"""
    project = _project(db, project_id)
    doc = _active_doc(db, project)
    from app.services.provider_service import ensure_verified_provider

    ensure_verified_provider(db)
    from app.services.translation_batch import expand_translation_task

    task = create_task(
        db,
        project_id,
        "translate",
        {
            "kind": "batch",
            "mode": payload.get("mode", "guided"),
            "source_document_id": doc.id,
            "model": payload.get("model", "fake-model"),
        },
    )
    count = expand_translation_task(db, task, doc.id)
    task.progress_json = json.dumps({"done": 0, "total": count})
    db.commit()
    return {"task_id": task.id, "windows": count}
