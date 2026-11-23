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
        raise HTTPException(status_code=404, detail="项目不存在")
    return p


def _active_doc(db: Session, project: Project) -> SourceDocument:
    if project.active_source_document_id is None:
        raise HTTPException(status_code=404, detail="尚未解析原文")
    return db.get(SourceDocument, project.active_source_document_id)


@router.post("/translation/express")
def start_express(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """一键直出：导入后串联全部环节，全程无人工干预。"""
    project = _project(db, project_id)
    from app.api.v1.translation import _ensure_no_running_task

    _ensure_no_running_task(db, project_id)
    doc = _active_doc(db, project)
    from app.services.provider_service import default_model_name, ensure_verified_provider

    ensure_verified_provider(db)
    task = create_task(
        db,
        project_id,
        "express",
        {"source_document_id": doc.id, "model": payload.get("model") or default_model_name(db)},
    )
    launch(task.id)
    return {"task_id": task.id}


@router.post("/approve/batch")
def batch_approve(project_id: str, db: Session = Depends(get_db)) -> dict:
    """自动定稿：只批准通过项目级自动质量门禁的当前译文。"""
    _project(db, project_id)
    from app.services.quality_service import auto_approve_project

    return auto_approve_project(db, project_id)


@router.get("/quality-report")
def quality_report(project_id: str, db: Session = Depends(get_db)) -> dict:
    """项目级自动质检报告：不依赖人工审核。"""
    _project(db, project_id)
    from app.services.quality_service import project_quality_report

    return project_quality_report(db, project_id)


@router.get("/qa-issues")
def list_issues(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    from app.models.document import SourceSegment
    from app.services.qa_service import refresh_forced_term_issues

    refresh_forced_term_issues(db, project_id)

    polished = list(db.scalars(
        select(Translation)
        .join(SourceSegment, Translation.segment_id == SourceSegment.id)
        .where(SourceSegment.project_id == project_id, Translation.stage == "polished")
    ))
    latest: dict[str, Translation] = {}
    for t in polished:
        current = latest.get(t.segment_id)
        if current is None or t.version > current.version:
            latest[t.segment_id] = t
    current_ids = {t.id for t in latest.values()}
    rows = db.scalars(
        select(QAIssue)
        .where(QAIssue.project_id == project_id)
        .order_by(QAIssue.created_at.desc())
    )
    rows = [i for i in rows if i.translation_id in current_ids]
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
    """导出剧本。

    payload.delivery（默认 true）：交付版（剧本交付模板.md，中文场景/动作/音效 + 英文台词，
    title_en 为可选英文剧名）；false：常规导出（bilingual × md/txt/docx）。
    """
    from app.models.document import SourceDocument

    project = _project(db, project_id)
    doc_title = None
    if project.active_source_document_id:
        doc = db.get(SourceDocument, project.active_source_document_id)
        if doc is not None:
            # 中文剧名取文件名（去掉扩展名）
            import os

            doc_title = os.path.splitext(doc.filename or "")[0] or None
    title_en = str(payload.get("title_en") or "").strip() or None
    delivery = bool(payload.get("delivery", True))
    if delivery and not bool(payload.get("allow_draft", False)):
        from app.services.quality_service import project_quality_report

        quality = project_quality_report(db, project.id)
        if quality.get("gate") != "passed":
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        "自动质检未通过，不能导出交付版："
                        + str(quality.get("reason") or "存在未解决问题")
                        + "。如需临时查看，可使用普通中英对照导出。"
                    ),
                    "quality": quality,
                },
            )
    return JSONResponse(
        export_project(
            db, project,
            bilingual=bool(payload.get("bilingual", True)),
            fmt=str(payload.get("format", "md")).lower(),
            title_en=title_en,
            doc_title=doc_title,
            delivery=delivery,
        )
    )
