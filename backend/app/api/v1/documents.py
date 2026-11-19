"""原文导入与解析路由。解析同步返回（几秒内完成）。"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.document import Chapter, Scene, SourceDocument, SourceSegment
from app.models.project import Project
from app.schemas.document import (
    ChapterOut,
    DocumentOut,
    ParseReportOut,
    SceneOut,
    SegmentOut,
)
from app.services import importer
from app.services.parse_service import parse_and_persist

router = APIRouter(prefix="/projects/{project_id}", tags=["documents"])


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.post("/documents/import", response_model=DocumentOut, status_code=201)
async def import_file(
    project_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> SourceDocument:
    project = _get_project(db, project_id)
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    try:
        from app.core.config import settings

        tmp = settings.data_dir / "uploads"
        tmp.mkdir(parents=True, exist_ok=True)
        raw = tmp / f"{file.filename}"
        raw.write_bytes(await file.read())
        doc = importer.import_document(db, project, raw, file.filename)
        return doc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(project_id: str, db: Session = Depends(get_db)) -> list[SourceDocument]:
    _get_project(db, project_id)
    return list(
        db.scalars(
            select(SourceDocument)
            .where(SourceDocument.project_id == project_id)
            .order_by(SourceDocument.version.desc())
        )
    )


@router.post("/parse")
def parse_document(project_id: str, db: Session = Depends(get_db)) -> dict:
    """同步解析当前项目最新文档版本。"""
    project = _get_project(db, project_id)
    doc = db.scalar(
        select(SourceDocument)
        .where(SourceDocument.project_id == project_id)
        .order_by(SourceDocument.version.desc())
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="尚未导入原文")
    text = importer.read_normalized(doc)
    result = parse_and_persist(db, doc, text)
    project.active_source_document_id = doc.id
    db.commit()
    return {"document_id": doc.id, "coverage_ok": result.report.coverage_ok}


@router.get("/parse-report")
def parse_report(project_id: str, db: Session = Depends(get_db)) -> dict:
    """解析报告：从当前片段统计生成。"""
    project = _get_project(db, project_id)
    doc_id = project.active_source_document_id
    if doc_id is None:
        raise HTTPException(status_code=404, detail="尚未解析")
    segs = list(
        db.scalars(
            select(SourceSegment).where(SourceSegment.source_document_id == doc_id)
        )
    )
    doc = db.get(SourceDocument, doc_id)
    text = importer.read_normalized(doc)
    covered = sum(s.source_end - s.source_start for s in segs)
    # 顺序检查
    anomalies: list[str] = []
    prev = 0
    for s in sorted(segs, key=lambda x: x.segment_no):
        if s.source_start != prev:
            anomalies.append(f"覆盖缺口：期望 {prev}，实际 {s.source_start}")
        prev = s.source_end
    if prev != len(text):
        anomalies.append(f"覆盖缺口：末尾期望 {len(text)}，实际 {prev}")
    report = {
        "total_chars": len(text),
        "covered_chars": covered,
        "uncovered_chars": max(0, len(text) - covered),
        "chapter_count": len(set(s.chapter_id for s in segs if s.chapter_id)),
        "scene_count": len(set(s.scene_id for s in segs if s.scene_id)),
        "dialogue_count": sum(1 for s in segs if s.segment_type == "dialogue"),
        "narrative_count": sum(1 for s in segs if s.segment_type == "narrative"),
        "heading_count": sum(1 for s in segs if s.segment_type == "heading"),
        "other_count": sum(1 for s in segs if s.segment_type == "other"),
        "anomalies": anomalies,
        "parser_version": "1.0.0",
        "coverage_ok": prev == len(text) and not anomalies,
    }
    return report


@router.get("/chapters", response_model=list[ChapterOut])
def list_chapters(project_id: str, db: Session = Depends(get_db)) -> list[Chapter]:
    project = _get_project(db, project_id)
    doc_id = project.active_source_document_id
    if doc_id is None:
        return []
    return list(
        db.scalars(
            select(Chapter)
            .where(Chapter.source_document_id == doc_id)
            .order_by(Chapter.chapter_no)
        )
    )


@router.get("/scenes", response_model=list[SceneOut])
def list_scenes(project_id: str, db: Session = Depends(get_db)) -> list[Scene]:
    project = _get_project(db, project_id)
    doc_id = project.active_source_document_id
    if doc_id is None:
        return []
    return list(
        db.scalars(
            select(Scene)
            .where(Scene.source_document_id == doc_id)
            .order_by(Scene.chapter_id, Scene.scene_no)
        )
    )


@router.get("/segments", response_model=list[SegmentOut])
def list_segments(project_id: str, db: Session = Depends(get_db)) -> list[SourceSegment]:
    project = _get_project(db, project_id)
    doc_id = project.active_source_document_id
    if doc_id is None:
        return []
    return list(
        db.scalars(
            select(SourceSegment)
            .where(SourceSegment.source_document_id == doc_id)
            .order_by(SourceSegment.segment_no)
        )
    )
