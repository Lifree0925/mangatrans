"""批量翻译：窗口规划、任务展开、窗口执行、carry-over 串行传递、stale 管理。"""

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.registry import get_provider
from app.models.document import SourceDocument, SourceSegment
from app.models.knowledge import KnowledgeItem, SceneContextCard
from app.models.task import Task, TaskItem
from app.models.translation import StyleGuide, Translation
from app.services.context_builder import build_context
from app.services.importer import sha256
from app.services.translation import PROMPT_VERSION, translate_window, validate_window
from app.services.window_planner import build_windows


def expand_translation_task(
    db: Session, task: Task, source_document_id: str, segment_subset: list[str] | None = None
) -> int:
    """把任务展开为窗口级 task_items（串行执行顺序即窗口顺序）。

    segment_subset：增量模式只翻译指定片段（保持原文顺序）。
    """
    segs = list(
        db.scalars(
            select(SourceSegment)
            .where(SourceSegment.source_document_id == source_document_id)
            .order_by(SourceSegment.segment_no)
        )
    )
    if segment_subset is not None:
        subset = set(segment_subset)
        segs = [s for s in segs if s.id in subset]
    windows = build_windows(segs)
    for i, window in enumerate(windows):
        db.add(
            TaskItem(
                task_id=task.id,
                segment_id=window[0].id,
                window_id=f"WIN_{i:04d}",
                window_segments_json=json.dumps([s.id for s in window]),
            )
        )
    db.commit()
    return len(windows)


def _load_window_context(db: Session, project_id: str, window: list[SourceSegment]) -> tuple:
    from app.models.storyline import ChapterStoryline

    first = window[0]
    card_row = db.scalar(
        select(SceneContextCard)
        .where(SceneContextCard.scene_id == first.scene_id)
        .order_by(SceneContextCard.version.desc())
    ) if first.scene_id else None
    card = json.loads(card_row.card_json) if card_row else None
    if card is not None:
        card["id"] = card_row.id

    # 当前章节剧情卡
    story_row = db.scalar(
        select(ChapterStoryline)
        .where(ChapterStoryline.chapter_id == first.chapter_id)
        .order_by(ChapterStoryline.version.desc())
    ) if first.chapter_id else None
    storyline = json.loads(story_row.card_json) if story_row else None
    if storyline is not None:
        storyline["id"] = story_row.id

    items = [
        {
            "id": k.id,
            "item_type": k.item_type,
            "canonical_key": k.canonical_key,
            "data": json.loads(k.data_json or "{}"),
        }
        for k in db.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.project_id == project_id,
                KnowledgeItem.status.in_(["approved", "auto_approved"]),
            )
        )
    ]
    return card, items, storyline


def _persist_window_translations(
    db: Session, window: list[SourceSegment], draft, polished, context, model: str
) -> None:
    for src, dr, po in zip(window, draft, polished):
        base = (
            db.scalar(
                select(func.max(Translation.version)).where(
                    Translation.segment_id == src.id
                )
            )
            or 0
        )
        for stage, entry in (("draft", dr), ("polished", po)):
            base += 1
            db.add(
                Translation(
                    segment_id=src.id,
                    version=base,
                    stage=stage,
                    target_text=entry["target_text"],
                    notes_json=json.dumps(entry.get("notes", []), ensure_ascii=False),
                    ambiguity_json=json.dumps(entry.get("ambiguity", []), ensure_ascii=False),
                    model_name=model,
                    provider_id="default",
                    prompt_version=PROMPT_VERSION,
                    context_snapshot_json=json.dumps(context.snapshot, ensure_ascii=False),
                    source_hash=sha256(src.source_text),
                )
            )


async def translate_window_direct(
    db_factory, project_id: str, window: list[SourceSegment], carry_over: dict | None, model: str
) -> tuple:
    """直接执行一个窗口（不依赖 task_item），返回 (carry_over, draft, polished, context)。"""
    with db_factory() as db:
        card, knowledge, storyline = _load_window_context(db, project_id, window)
        sg = db.scalar(
            select(StyleGuide)
            .where(StyleGuide.project_id == project_id)
            .order_by(StyleGuide.version.desc())
        )
        style_guide = json.loads(sg.guide_json) if sg else None

    seg_dicts = [
        {
            "segment_id": s.id,
            "segment_no": s.segment_no,
            "segment_type": s.segment_type,
            "speaker_name": s.speaker_name,
            "source_text": s.source_text,
        }
        for s in window
    ]
    context = build_context(
        segments=seg_dicts,
        scene_card=card,
        knowledge=knowledge,
        style_guide=style_guide,
        carry_over=carry_over,
        storyline=storyline,
    )
    draft, polished, carry = await translate_window(
        provider=get_provider(), model=model, window_id="EXPRESS", context=context
    )
    return carry, draft, polished, context


async def process_window_item(db_factory, task_id: str, item_id: str, model: str) -> dict:
    """执行一个翻译窗口：读上下文 → 两段式 → 校验 → 事务提交。"""
    with db_factory() as db:
        task = db.get(Task, task_id)
        item = db.get(TaskItem, item_id)
        if task is None or item is None:
            return {"error": "task/item not found"}
        seg_ids = json.loads(item.window_segments_json or "[]")
        window = list(db.scalars(select(SourceSegment).where(SourceSegment.id.in_(seg_ids))))
        window.sort(key=lambda s: seg_ids.index(s.id))
        progress = json.loads(task.progress_json or "{}")
        carry_over = progress.get("carry_over")
        sg = db.scalar(
            select(StyleGuide)
            .where(StyleGuide.project_id == task.project_id)
            .order_by(StyleGuide.version.desc())
        )
        style_guide = json.loads(sg.guide_json) if sg else None
        card, knowledge, storyline = _load_window_context(db, task.project_id, window)

    seg_dicts = [
        {
            "segment_id": s.id,
            "segment_no": s.segment_no,
            "segment_type": s.segment_type,
            "speaker_name": s.speaker_name,
            "source_text": s.source_text,
        }
        for s in window
    ]
    context = build_context(
        segments=seg_dicts,
        scene_card=card,
        knowledge=knowledge,
        style_guide=style_guide,
        carry_over=carry_over,
        storyline=storyline,
    )

    provider = get_provider()
    draft, polished, carry = await translate_window(
        provider=provider,
        model=model,
        window_id=item.window_id or "",
        context=context,
    )
    errors = validate_window(seg_dicts, polished)
    if errors:
        return {"error": "; ".join(errors)}

    with db_factory() as db:
        _persist_window_translations(db, window, draft, polished, context, model)
        # 串行推进：更新任务级 carry-over 与进度
        task = db.get(Task, task_id)
        progress = json.loads(task.progress_json or "{}")
        progress["carry_over"] = carry
        done = int(progress.get("done", 0)) + 1
        progress["done"] = done
        total = int(progress.get("total", 0)) or db.scalar(
            select(func.count()).select_from(TaskItem).where(TaskItem.task_id == task_id)
        ) or done
        progress["total"] = total
        task.progress_json = json.dumps(progress, ensure_ascii=False)
        task.last_checkpoint = item.window_id
        item = db.get(TaskItem, item_id)
        item.status = "succeeded"
        item.result_id = item.id
        item.finished_at = func.now()
        db.commit()
    return {"done": done}


def mark_stale_for_knowledge(db: Session, knowledge_id: str) -> int:
    """知识变化后标记受影响译文 stale。"""
    rows = list(
        db.scalars(select(Translation).where(Translation.freshness_status == "current"))
    )
    touched = 0
    for t in rows:
        snap = json.loads(t.context_snapshot_json or "{}")
        if knowledge_id in snap.get("knowledge_ids", []):
            t.freshness_status = "stale"
            touched += 1
    db.commit()
    return touched


def mark_stale_for_style_guide(db: Session, project_id: str) -> int:
    rows = list(
        db.scalars(
            select(Translation)
            .join(SourceSegment, Translation.segment_id == SourceSegment.id)
            .where(
                SourceSegment.project_id == project_id,
                Translation.freshness_status == "current",
            )
        )
    )
    for t in rows:
        t.freshness_status = "stale"
    db.commit()
    return len(rows)
