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
from app.parsers.script_parser import PARSER_VERSION

router = APIRouter(prefix="/projects/{project_id}", tags=["documents"])


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
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
    # 文件名消毒：只取纯文件名，防路径穿越（../ 与盘符路径）
    safe_name = file.filename.replace("\\", "/").split("/")[-1].strip()
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="非法文件名")
    try:
        from app.core.config import settings

        tmp = settings.data_dir / "uploads"
        tmp.mkdir(parents=True, exist_ok=True)
        raw = tmp / safe_name
        raw.write_bytes(await file.read())
        doc = importer.import_document(db, project, raw, safe_name)
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
    """创建解析任务（异步）：解析报告在任务中心展示。"""
    from app.pipeline.runner import create_task, launch
    from app.services.provider_service import default_model_name

    project = _get_project(db, project_id)
    from sqlalchemy import select as sa_select

    from app.models.task import Task

    running = db.scalar(
        sa_select(Task.id).where(
            Task.project_id == project_id, Task.status.in_(["running", "queued"])
        ).limit(1)
    )
    if running:
        raise HTTPException(
            status_code=409,
            detail="项目有进行中的任务，无法重新解析",
        )
    doc = db.scalar(
        select(SourceDocument)
        .where(SourceDocument.project_id == project_id)
        .order_by(SourceDocument.version.desc())
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="尚未导入原文")
    task = create_task(
        db,
        project_id,
        "parse",
        {"source_document_id": doc.id, "model": default_model_name(db)},
    )
    launch(task.id)
    return {"task_id": task.id}


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
        "parser_version": PARSER_VERSION,
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


@router.patch("/segments/{segment_id}")
def edit_segment(project_id: str, segment_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """人工编辑原文片段：留痕 + 旧译文标记待重译。"""
    from sqlalchemy import update

    from app.models.audit import AuditLog
    from app.models.translation import Translation
    from app.services.importer import sha256

    _get_project(db, project_id)
    seg = db.get(SourceSegment, segment_id)
    if seg is None or seg.project_id != project_id:
        raise HTTPException(status_code=404, detail="片段不存在")
    new_text = str(payload.get("source_text", ""))
    if not new_text.strip():
        raise HTTPException(status_code=400, detail="原文不能为空")
    old_text = seg.source_text
    db.add(AuditLog(
        project_id=project_id, entity_type="segment", entity_id=segment_id,
        action="edit_source", before_text=old_text, after_text=new_text))
    seg.source_text = new_text
    seg.source_hash = sha256(new_text)
    # 旧译文与修改后的原文不再对应：标记待重译
    stale = db.execute(
        update(Translation)
        .where(Translation.segment_id == segment_id, Translation.freshness_status == "current")
        .values(freshness_status="stale")
    ).rowcount
    db.commit()
    return {"id": seg.id, "source_text": seg.source_text, "stale_translations": stale}


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
