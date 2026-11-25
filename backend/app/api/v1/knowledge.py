"""知识、场景卡与提取任务 API。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.document import Chapter, SourceDocument, SourceSegment
from app.models.knowledge import KnowledgeAlias, KnowledgeEvidence, KnowledgeItem, SceneContextCard
from app.models.project import Project
from app.models.storyline import ChapterStoryline
from app.services.provider_service import default_model_name
from app.pipeline.runner import create_task, launch
from app.schemas.knowledge import KnowledgeOut

router = APIRouter(prefix="/projects/{project_id}", tags=["knowledge"])
overview_router = APIRouter(tags=["knowledge"])


@overview_router.get("/knowledge/overview")
def knowledge_overview(db: Session = Depends(get_db)) -> dict:
    """知识库总览：仅列出已有知识库数据的项目（一键直出后自动出现）。"""
    projects = list(db.scalars(select(Project).order_by(Project.updated_at.desc())))

    kind_counts: dict[str, dict[str, int]] = {}
    for pid, item_type, n in db.execute(
        select(KnowledgeItem.project_id, KnowledgeItem.item_type, func.count())
        .group_by(KnowledgeItem.project_id, KnowledgeItem.item_type)
    ).all():
        kind_counts.setdefault(pid, {})[item_type or ""] = int(n)

    story_counts = {
        pid: int(n)
        for pid, n in db.execute(
            select(ChapterStoryline.project_id, func.count())
            .group_by(ChapterStoryline.project_id)
        ).all()
    }
    docs = {d.id: d for d in db.scalars(select(SourceDocument))}

    items = []
    for p in projects:
        counts = kind_counts.get(p.id, {})
        storylines = story_counts.get(p.id, 0)
        if not counts and not storylines:
            continue  # 无知识库数据的项目不显示（入口自动生成）
        doc = docs.get(p.active_source_document_id) if p.active_source_document_id else None
        items.append({
            "project_id": p.id,
            "project_name": p.name,
            "filename": doc.filename if doc else None,
            "characters": counts.get("character", 0),
            "terms": counts.get("term", 0),
            "rules": counts.get("rule", 0),
            "storylines": storylines,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        })
    return {"items": items}


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


@router.get("/knowledge")
def list_knowledge(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """知识条目列表：角色按剧本出现次数降序，其余按更新时间。"""
    from sqlalchemy import func as _func

    _project(db, project_id)
    items = list(
        db.scalars(
            select(KnowledgeItem).where(KnowledgeItem.project_id == project_id)
        )
    )
    # 角色出现次数：直接按原文片段的说话人统计，避免维护重复的台词本表。
    speak_counts: dict[str, int] = dict(
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
        effective_policy = None
        if i.item_type == "term":
            import json as _json

            from app.services.term_match import enforcement_for

            try:
                data = _json.loads(i.data_json or "{}")
            except Exception:  # noqa: BLE001 损坏数据不阻塞列表
                data = {}
            effective_policy = enforcement_for(i.canonical_key, data)
        return {
            "id": i.id,
            "item_type": i.item_type,
            "canonical_key": i.canonical_key,
            "data_json": i.data_json,
            "status": i.status,
            "source": i.source,
            "version": i.version,
            "occurrence_count": occ(i) if i.item_type == "character" else None,
            "effective_policy": effective_policy,
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
    only_weight = bool(data_payload) and set(data_payload.keys()) <= {"role_weight"}
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
    from app.services.context_cache import invalidate_project_knowledge

    invalidate_project_knowledge(project_id)


@router.delete("/knowledge", status_code=204)
def delete_all_knowledge(project_id: str, db: Session = Depends(get_db)) -> None:
    """硬删除项目全部知识库数据（角色/术语/规则、别名/证据、场景卡、章节剧情）。

    保留项目、原文、译文与任务记录；重新一键直出可再次生成。
    """
    _project(db, project_id)
    item_ids = select(KnowledgeItem.id).where(KnowledgeItem.project_id == project_id)
    db.execute(delete(KnowledgeEvidence).where(KnowledgeEvidence.knowledge_id.in_(item_ids)))
    db.execute(delete(KnowledgeAlias).where(KnowledgeAlias.knowledge_id.in_(item_ids)))
    db.execute(delete(KnowledgeItem).where(KnowledgeItem.project_id == project_id))
    db.execute(delete(SceneContextCard).where(SceneContextCard.project_id == project_id))
    db.execute(delete(ChapterStoryline).where(ChapterStoryline.project_id == project_id))
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
    if item_type == "term" and data:
        from app.services.term_match import classify_term, enforcement_for

        data["term_kind"] = data.get("term_kind") or classify_term(key, data)
        data["enforcement"] = data.get("enforcement") or enforcement_for(key, data)
    existing = db.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.project_id == project_id,
            KnowledgeItem.item_type == item_type,
            KnowledgeItem.canonical_key == key,
        ).limit(1)
    )
    if existing is not None:
        merged = json.loads(existing.data_json or "{}")
        merged.update(data)
        existing.data_json = json.dumps(merged, ensure_ascii=False)
        existing.status = payload.get("status", existing.status)
        existing.version += 1
        item = existing
        db.commit()
        db.refresh(item)
        return {"id": item.id, "item_type": item.item_type, "canonical_key": item.canonical_key,
                "status": item.status, "data_json": item.data_json, "updated": True}
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
    try:
        from app.services import continuity_service

        chapter = db.get(Chapter, row.chapter_id)
        if chapter is not None:
            continuity_service.sync_storyline(
                db, project_id, chapter, row, _json.loads(row.card_json or "{}")
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 剧情卡已保存，派生状态失败不应回滚人工编辑
        db.rollback()
        print(f"backend: continuity sync failed after storyline edit: {exc}", flush=True)
    return {"id": row.id, "status": row.status, "version": row.version}


@router.get("/character-states")
def list_character_states(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """章节级人物状态时间线（仅 current 版本）。"""
    import json as _json

    from app.models.continuity import CharacterState

    _project(db, project_id)
    rows = list(db.scalars(
        select(CharacterState)
        .where(
            CharacterState.project_id == project_id,
            CharacterState.status == "current",
        )
        .order_by(CharacterState.chapter_no, CharacterState.character_key)
    ))
    return [
        {
            "id": row.id,
            "character": row.character_key,
            "chapter_no": row.chapter_no,
            "version": row.version,
            "state": _json.loads(row.state_json or "{}"),
            "evidence": _json.loads(row.evidence_json or "{}"),
        }
        for row in rows
    ]


@router.get("/story-facts")
def list_story_facts(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """关键事实表：按生效章节排序，供知识库审计。"""
    import json as _json

    from app.models.continuity import StoryFact

    _project(db, project_id)
    rows = list(db.scalars(
        select(StoryFact)
        .where(StoryFact.project_id == project_id, StoryFact.status == "current")
        .order_by(StoryFact.chapter_no, StoryFact.fact_type)
    ))
    return [
        {
            "id": row.id,
            "chapter_no": row.chapter_no,
            "fact_type": row.fact_type,
            "subject": row.subject,
            "predicate": row.predicate,
            "object": row.object_text,
            "text": row.fact_text,
            "valid_from_chapter_no": row.valid_from_chapter_no,
            "valid_to_chapter_no": row.valid_to_chapter_no,
            "reveal_chapter_no": row.reveal_chapter_no,
            "evidence": _json.loads(row.evidence_json or "{}"),
        }
        for row in rows
    ]
