"""知识、场景卡与提取任务 API。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.document import Chapter, SourceDocument
from app.models.knowledge import KnowledgeItem, SceneContextCard
from app.models.project import Project
from app.services.provider_service import default_model_name
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
            "model": payload.get("model") or default_model_name(db),
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
        {"source_document_id": doc.id, "model": payload.get("model") or default_model_name(db)},
    )
    launch(task.id)
    return {"task_id": task.id}


@router.get("/knowledge")
def list_knowledge(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """知识条目列表：角色按剧本出现次数降序，其余按更新时间。"""
    from sqlalchemy import func as _func

    from app.models.dialogue import DialogueLine

    _project(db, project_id)
    items = list(
        db.scalars(
            select(KnowledgeItem).where(KnowledgeItem.project_id == project_id)
        )
    )
    # 角色出现次数：台词本说话人计数（无台词本时回退片段说话人计数）
    speak_counts: dict[str, int] = dict(
        db.execute(
            select(DialogueLine.speaker_name, _func.count())
            .where(DialogueLine.project_id == project_id)
            .group_by(DialogueLine.speaker_name)
        ).all()
    )
    if not speak_counts:
        from app.models.document import SourceSegment

        speak_counts = dict(
            db.execute(
                select(SourceSegment.speaker_name, _func.count())
                .where(SourceSegment.project_id == project_id, SourceSegment.speaker_name.is_not(None))
                .group_by(SourceSegment.speaker_name)
            ).all()
        )

    def occ(item: KnowledgeItem) -> int:
        n = speak_counts.get(item.canonical_key, 0)
        if n == 0:
            try:
                import json as _json

                aliases = _json.loads(item.data_json or "{}").get("aliases") or []
                for a in aliases:
                    n += speak_counts.get(a, 0)
            except Exception:  # noqa: BLE001
                pass
        return n

    chars = sorted(
        (i for i in items if i.item_type == "character"),
        key=lambda i: (occ(i), i.updated_at or ""),
        reverse=True,
    )
    others = sorted(
        (i for i in items if i.item_type != "character"),
        key=lambda i: i.updated_at or "",
        reverse=True,
    )
    ordered = chars + others

    def dump(i: KnowledgeItem) -> dict:
        return {
            "id": i.id,
            "item_type": i.item_type,
            "canonical_key": i.canonical_key,
            "data_json": i.data_json,
            "status": i.status,
            "source": i.source,
            "version": i.version,
            "occurrence_count": occ(i) if i.item_type == "character" else None,
        }

    return [dump(i) for i in ordered]


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
    if "data" in payload and isinstance(payload["data"], dict):
        # 合并 data（如 {"translation": "..."}），保留其它字段
        import json

        merged = dict(json.loads(item.data_json or "{}"))
        merged.update(payload["data"])
        item.data_json = json.dumps(merged, ensure_ascii=False)
        item.version += 1
    db.commit()
    stale = 0
    # role_weight 仅影响前台分组展示，不影响翻译，不触发译文待重译
    data_payload = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    only_weight = set(data_payload.keys()) <= {"role_weight"}
    if any(k in payload for k in ("status", "canonical_key", "data")) and not only_weight:
        from app.services.translation_batch import mark_stale_for_knowledge

        stale = mark_stale_for_knowledge(db, item.id)
    return {"id": item.id, "status": item.status, "version": item.version, "stale_translations": stale}


@router.delete("/knowledge/{item_id}", status_code=204)
def delete_knowledge(project_id: str, item_id: str, db: Session = Depends(get_db)) -> None:
    """删除知识条目（别名/证据级联删除）。"""
    from sqlalchemy import delete

    from app.models.knowledge import KnowledgeAlias, KnowledgeEvidence

    item = db.get(KnowledgeItem, item_id)
    if item is None or item.project_id != project_id:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    db.execute(delete(KnowledgeAlias).where(KnowledgeAlias.knowledge_id == item_id))
    db.execute(delete(KnowledgeEvidence).where(KnowledgeEvidence.knowledge_id == item_id))
    db.delete(item)
    db.commit()


@router.post("/knowledge", status_code=201)
def add_knowledge(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """手动添加知识条目（例如翻译人名/术语）。payload: {item_type, canonical_key, translation?, status?}"""
    import json

    project = _project(db, project_id)
    item_type = payload.get("item_type", "term")
    if item_type not in ("character", "term", "relation", "rule"):
        raise HTTPException(status_code=400, detail="类型只能是 人物/术语/关系/规则")
    key = str(payload.get("canonical_key", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="条目名称不能为空")
    translation = str(payload.get("translation", "")).strip()
    data = {"translation": translation} if translation else {}
    item = KnowledgeItem(
        project_id=project_id,
        item_type=item_type,
        canonical_key=key,
        data_json=json.dumps(data, ensure_ascii=False),
        status=payload.get("status", "approved"),
        source="manual",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "item_type": item.item_type, "canonical_key": item.canonical_key,
            "status": item.status, "data_json": item.data_json}


@router.get("/storylines")
def list_storylines(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """章节剧情卡列表（含章节号与标题，最新版本）。"""
    import json as _json

    from app.models.storyline import ChapterStoryline

    _project(db, project_id)
    chapters = {c.id: c for c in db.scalars(select(Chapter).where(Chapter.project_id == project_id))}
    rows = list(
        db.scalars(
            select(ChapterStoryline)
            .where(ChapterStoryline.project_id == project_id)
            .order_by(ChapterStoryline.updated_at.desc())
        )
    )
    latest: dict[str, dict] = {}
    for r in rows:
        if r.chapter_id not in latest:
            latest[r.chapter_id] = {
                "id": r.id,
                "chapter_id": r.chapter_id,
                "chapter_no": chapters[r.chapter_id].chapter_no if r.chapter_id in chapters else 0,
                "chapter_title": (chapters[r.chapter_id].title or "") if r.chapter_id in chapters else "",
                "card_json": r.card_json,
                "status": r.status,
                "version": r.version,
            }
    return sorted(latest.values(), key=lambda x: x["chapter_no"])


@router.patch("/storylines/{storyline_id}")
def patch_storyline(project_id: str, storyline_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """人工修改章节剧情卡（重要情节/伏笔等）。"""
    import json as _json

    from app.models.storyline import ChapterStoryline

    row = db.get(ChapterStoryline, storyline_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="剧情卡不存在")
    if "card" in payload and isinstance(payload["card"], dict):
        row.card_json = _json.dumps(payload["card"], ensure_ascii=False)
        row.status = "edited"
        row.version += 1
    db.commit()
    return {"id": row.id, "status": row.status, "version": row.version}


@router.get("/scene-cards", response_model=list[SceneCardOut])
def list_scene_cards(project_id: str, db: Session = Depends(get_db)) -> list[SceneContextCard]:
    return list(
        db.scalars(
            select(SceneContextCard)
            .where(SceneContextCard.project_id == project_id)
            .order_by(SceneContextCard.updated_at.desc())
        )
    )
