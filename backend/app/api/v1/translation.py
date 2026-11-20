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
        raise HTTPException(status_code=404, detail="项目不存在")
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
        raise HTTPException(status_code=404, detail="风格基线不存在")
    if "guide" in payload:
        sg.guide_json = json.dumps(payload["guide"], ensure_ascii=False)
    db.commit()
    return {"id": sg.id, "version": sg.version, "status": sg.status}


@router.post("/segments/{segment_id}/translations", status_code=201)
def add_manual_translation(project_id: str, segment_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """人工校对译文：新增版本（不覆盖旧译文），直接标记 approved 且与当前原文对齐。"""
    from app.models.audit import AuditLog

    project = _project(db, project_id)
    seg = db.get(SourceSegment, segment_id)
    if seg is None or seg.project_id != project_id:
        raise HTTPException(status_code=404, detail="片段不存在")
    text = str(payload.get("target_text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="译文不能为空")
    base = db.scalar(
        select(func.max(Translation.version)).where(Translation.segment_id == segment_id)
    ) or 0
    t = Translation(
        segment_id=segment_id,
        version=base + 1,
        stage="polished",
        target_text=text,
        review_status="approved",
        freshness_status="current",
        model_name="manual",
        provider_id="manual",
        prompt_version="manual",
        source_hash=seg.source_hash,
    )
    db.add(t)
    db.add(AuditLog(
        project_id=project_id, entity_type="translation", entity_id=segment_id,
        action="add_translation", before_text=None, after_text=text))
    db.commit()
    db.refresh(t)
    return {"id": t.id, "version": t.version, "target_text": t.target_text,
            "review_status": t.review_status, "freshness_status": t.freshness_status}


@router.get("/translations/latest")
def list_latest_translations(project_id: str, db: Session = Depends(get_db)) -> dict:
    """批量返回每片段最新 polished 译文（含待重译标记），供对照视图使用。"""
    from app.models.document import SourceSegment

    _project(db, project_id)
    segs = db.scalars(
        select(SourceSegment).where(SourceSegment.project_id == project_id)
    ).all()
    rows = db.scalars(
        select(Translation)
        .where(Translation.segment_id.in_([s.id for s in segs]), Translation.stage == "polished")
        .order_by(Translation.version.desc())
    ).all()
    latest: dict[str, dict] = {}
    for t in rows:
        if t.segment_id not in latest:
            latest[t.segment_id] = {
                "segment_id": t.segment_id,
                "target_text": t.target_text,
                "review_status": t.review_status,
                "freshness_status": t.freshness_status,
                "version": t.version,
                "model_name": t.model_name,
            }
    return {"items": list(latest.values())}


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
