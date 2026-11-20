"""QA、批量接受与导出 API。"""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.document import SourceDocument
from app.models.project import Project
from app.models.qa import QAIssue
from app.models.translation import Translation
from app.pipeline.runner import create_task, launch
from app.services.exporter import export_project

router = APIRouter(prefix="/projects/{project_id}", tags=["qa"])


def _project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")
    return p


def _active_doc(db: Session, project: Project) -> SourceDocument:
    if project.active_source_document_id is None:
        raise HTTPException(status_code=404, detail="尚未解析原文")
    return db.get(SourceDocument, project.active_source_document_id)


@router.post("/translation/express")
def start_express(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """一键直出：导入后串联全部环节，全程无人工干预。"""
    project = _project(db, project_id)
    doc = _active_doc(db, project)
    from app.services.provider_service import ensure_verified_provider

    ensure_verified_provider(db)
    task = create_task(
        db,
        project_id,
        "express",
        {"source_document_id": doc.id, "model": payload.get("model", "fake-model")},
    )
    launch(task.id)
    return {"task_id": task.id}


@router.post("/approve/batch")
def batch_approve(project_id: str, db: Session = Depends(get_db)) -> dict:
    """批量接受：polished 且无 high/critical 问题的译文标记 approved。"""
    _project(db, project_id)
    high_ids = {
        i.translation_id
        for i in db.scalars(
            select(QAIssue).where(
                QAIssue.project_id == project_id,
                QAIssue.severity.in_(["high", "critical"]),
            )
        )
    }
    from app.models.document import SourceSegment

    rows = list(
        db.scalars(
            select(Translation)
            .join(SourceSegment, Translation.segment_id == SourceSegment.id)
            .where(
                SourceSegment.project_id == project_id,
                Translation.stage == "polished",
            )
        )
    )
    approved = 0
    skipped = 0
    for t in rows:
        if t.review_status == "approved":
            continue
        if t.id in high_ids:
            skipped += 1
            continue
        t.review_status = "approved"
        approved += 1
    db.commit()
    return {"approved": approved, "skipped_high_severity": skipped}


@router.get("/qa-issues")
def list_issues(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(QAIssue)
        .where(QAIssue.project_id == project_id)
        .order_by(QAIssue.created_at.desc())
    )
    return [
        {
            "id": i.id,
            "issue_type": i.issue_type,
            "severity": i.severity,
            "source_quote": i.source_quote,
            "target_quote": i.target_quote,
            "explanation": i.explanation,
            "status": i.status,
        }
        for i in rows
    ]


@router.post("/export")
def export_project_route(
    project_id: str, payload: dict, db: Session = Depends(get_db)
) -> JSONResponse:
    """导出：format = md / txt / docx；bilingual 控制中英对照或纯英文。"""
    project = _project(db, project_id)
    bilingual = bool(payload.get("bilingual", True))
    fmt = str(payload.get("format", "md")).lower()
    return JSONResponse(export_project(db, project, bilingual, fmt))
