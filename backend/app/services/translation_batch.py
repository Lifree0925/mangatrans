"""批量翻译：窗口规划、任务展开、窗口执行、carry-over 串行传递、stale 管理。"""

import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.llm.registry import get_provider
from app.models.document import SourceDocument, SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.task import Task, TaskItem
from app.models.translation import StyleGuide, Translation
from app.services.context_builder import ContextResult, build_context
from app.services.importer import sha256
from app.services.translation import (
    PROMPT_VERSION, translate_window, translate_window_draft, translate_window_polish,
    validate_window,
)
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
    # 场景卡已从管线剔除（实测无法识别场景、无实质作用）；
    # 情境信息由知识库（角色/术语）、章节剧情卡与 carry-over 承担。
    card = None

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
            "aliases": [],
        }
        for k in db.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.project_id == project_id,
                KnowledgeItem.status.in_(["approved", "auto_approved"]),
            )
        )
    ]
    # 别名（如 允→王允、玄德→刘备）：翻译时统一为正式名
    from app.models.knowledge import KnowledgeAlias

    alias_rows = list(
        db.scalars(
            select(KnowledgeAlias).where(
                KnowledgeAlias.knowledge_id.in_([i["id"] for i in items])
            )
        )
    )
    for i in items:
        i["aliases"] = [a.alias for a in alias_rows if a.knowledge_id == i["id"]]
    return card, items, storyline


def _window_forced_terms(db: Session, project_id: str, window: list[SourceSegment]) -> dict[str, str]:
    """窗口内出现且有译名的术语 -> 强制译名。"""
    import json as _json

    win_text = "".join(s.source_text for s in window)
    from app.models.knowledge import KnowledgeItem

    forced: dict[str, str] = {}
    for k in db.scalars(
        select(KnowledgeItem).where(
            KnowledgeItem.project_id == project_id,
            KnowledgeItem.item_type == "term",
            KnowledgeItem.status.in_(["approved", "auto_approved"]),
        )
    ):
        translation = _json.loads(k.data_json or "{}").get("translation")
        if translation and translation != k.canonical_key and k.canonical_key in win_text:
            forced[k.canonical_key] = translation
    return forced


async def _qa_check_loop(
    *,
    db_factory,
    project_id: str,
    window: list[SourceSegment],
    draft: list[dict],
    polished: list[dict],
    carry: dict,
    context,
    provider,
    model: str,
    max_loops: int = 3,
) -> tuple[list[dict], dict, list[dict]]:
    """质检打回定点修复：QA 发现 high/critical 问题时，AI 只重译问题段（窗口其余段保持不变），
    每轮只修剩余问题段，最多 3 轮。返回 (最终 polished, carry, 最终问题列表)。
    """
    from app.services import qa_service
    from app.services.translation import repair_segments

    issues: list[dict] = []
    for _ in range(max_loops):
        with db_factory() as db:
            forced = _window_forced_terms(db, project_id, window)
            issues = []
            seg_high: dict[str, list[dict]] = {}
            for seg, tr in zip(window, polished):
                # 临时 Translation 对象用于 QA
                class _FakeTr:
                    target_text = tr["target_text"]
                det = qa_service.deterministic_qa(db, seg, _FakeTr(), forced)
                issues.extend(det)
                high_here = [i for i in det if i.get("severity") in ("high", "critical")]
                if high_here:
                    seg_high[seg.id] = high_here
        if not seg_high:
            break
        # 定点修复：只重译问题段；修复失败则保持原样退出（不整窗重做）
        try:
            fixed_map = await repair_segments(
                provider=provider, model=model, window_id="QA-FIX", context=context,
                draft=draft,
                issues=[i for lst in seg_high.values() for i in lst],
                seg_ids=sorted(seg_high),
            )
        except Exception:  # noqa: BLE001 定点修复异常：保持原译文，不阻塞窗口
            fixed_map = {}
        if not fixed_map:
            break
        for e in polished:
            if e.get("segment_id") in fixed_map:
                e["target_text"] = fixed_map[e["segment_id"]]
    return polished, carry, issues


def insert_translation_row(db: Session, *, segment_id: str, **kw) -> Translation:
    """并发安全的译文插入：UNIQUE(segment_id, version) 冲突时在 SAVEPOINT 内
    重读最大版本重试（最多 3 次），避免双任务并发写同版本撞约束。"""
    last_exc: IntegrityError | None = None
    for _ in range(3):
        try:
            with db.begin_nested():
                base = (
                    db.scalar(
                        select(func.max(Translation.version)).where(
                            Translation.segment_id == segment_id
                        )
                    )
                    or 0
                )
                t = Translation(segment_id=segment_id, version=base + 1, **kw)
                db.add(t)
                db.flush()
            return t
        except IntegrityError as exc:
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


def _persist_stage(
    db: Session, window: list[SourceSegment], entries: list[dict], stage: str,
    context, model: str,
) -> None:
    """按阶段入库：draft 与 polished 分别持久化（两轮都入库）。"""
    for src, entry in zip(window, entries):
        insert_translation_row(
            db,
            segment_id=src.id,
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


def _persist_window_translations(
    db: Session, window: list[SourceSegment], draft, polished, context, model: str
) -> None:
    """draft 与 polished 连续版本入库（共享 base 递增，避免版本冲突）。"""
    for src, dr, po in zip(window, draft, polished):
        for entry in (dr, po):
            insert_translation_row(
                db,
                segment_id=src.id,
                stage="draft" if entry is dr else "polished",
                target_text=entry["target_text"],
                notes_json=json.dumps(entry.get("notes", []), ensure_ascii=False),
                ambiguity_json=json.dumps(entry.get("ambiguity", []), ensure_ascii=False),
                model_name=model,
                provider_id="default",
                prompt_version=PROMPT_VERSION,
                context_snapshot_json=json.dumps(context.snapshot, ensure_ascii=False),
                source_hash=sha256(src.source_text),
            )


async def _window_material(
    db_factory, project_id: str, window: list[SourceSegment], carry_over: dict | None
) -> tuple[list[dict], ContextResult, dict | None, dict | None]:
    """窗口公共材料：segment dicts、style guide、场景卡/知识/剧情卡、context。"""
    with db_factory() as db:
        _card, knowledge, storyline = _load_window_context(db, project_id, window)
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
        scene_card=_card,
        knowledge=knowledge,
        style_guide=style_guide,
        carry_over=carry_over,
        storyline=storyline,
    )
    return seg_dicts, context, style_guide, _card


async def translate_window_draft_direct(
    db_factory, project_id: str, window: list[SourceSegment], model: str
) -> tuple[list[dict], ContextResult]:
    """Draft 阶段（可并发）：不依赖 carry-over。返回 (draft, draft_carry, context)。"""
    seg_dicts, context, _sg, _card = await _window_material(db_factory, project_id, window, None)
    draft, carry = await translate_window_draft(
        provider=get_provider(), model=model, window_id="DRAFT", context=context
    )
    return draft, carry, context


async def translate_window_polish_direct(
    db_factory, project_id: str, window: list[SourceSegment], draft: list[dict],
    carry_over: dict | None, model: str,
) -> tuple[list[dict], dict]:
    """Polish 阶段（串行）：carry-over 入上下文，生成 polished + 新 carry。"""
    seg_dicts, context, _sg, _card = await _window_material(
        db_factory, project_id, window, carry_over
    )
    polished, carry = await translate_window_polish(
        provider=get_provider(), model=model, window_id="POLISH",
        context=context, draft=draft, carry_over=carry_over, segments=seg_dicts,
    )
    return polished, carry


async def process_window_draft_item(db_factory, task_id: str, item_id: str, model: str) -> dict:
    """Draft 阶段 item：并发执行（无 carry-over），完成后转入 polish 阶段。"""
    with db_factory() as db:
        task = db.get(Task, task_id)
        item = db.get(TaskItem, item_id)
        if task is None or item is None:
            return {"error": "task/item not found"}
        seg_ids = json.loads(item.window_segments_json or "[]")
        window = list(db.scalars(select(SourceSegment).where(SourceSegment.id.in_(seg_ids))))
        window.sort(key=lambda s: seg_ids.index(s.id))
        project_id = task.project_id

    draft, carry, context = await translate_window_draft_direct(db_factory, project_id, window, model)
    with db_factory() as db:
        _persist_stage(db, window, draft, "draft", context, model)
        item = db.get(TaskItem, item_id)
        item.phase = "polish"
        item.status = "pending"
        item.finished_at = func.now()
        db.commit()
    return {"done": 1, "window_id": item.window_id, "draft_carry": carry}


async def process_window_polish_item(db_factory, task_id: str, item_id: str, model: str) -> dict:
    """Polish 阶段 item：串行执行，carry-over 窗口间传递。"""
    with db_factory() as db:
        task = db.get(Task, task_id)
        item = db.get(TaskItem, item_id)
        if task is None or item is None:
            return {"error": "task/item not found"}
        seg_ids = json.loads(item.window_segments_json or "[]")
        window = list(db.scalars(select(SourceSegment).where(SourceSegment.id.in_(seg_ids))))
        window.sort(key=lambda s: seg_ids.index(s.id))
        progress = json.loads(task.progress_json or "{}")
        project_id = task.project_id
        # carry-over 前置：取前一窗口 Draft 生成的 carry（Polish 并发的前提）；
        # 旧任务无 draft carry 时回退 polish 输出链（串行兼容）。
        win_idx = int(item.window_id.split("_")[1])
        prev_win_id = f"WIN_{win_idx - 1:04d}" if win_idx > 0 else None
        draft_carry_map = progress.get("draft_carry") or {}
        carry_over = draft_carry_map.get(prev_win_id) if prev_win_id else None
        if carry_over is None and prev_win_id:
            carry_over = progress.get("carry_over")
        window_id = item.window_id
        # Draft 已入库：读本窗口最新 draft
        draft_rows = list(
            db.scalars(
                select(Translation)
                .where(
                    Translation.segment_id.in_([s.id for s in window]),
                    Translation.stage == "draft",
                )
                .order_by(Translation.version.desc())
            )
        )
        latest_draft: dict[str, Translation] = {}
        for t in draft_rows:
            latest_draft.setdefault(t.segment_id, t)
        if len(latest_draft) != len(window):
            return {"error": "draft 缺失，窗口需重新执行 draft 阶段"}
        draft = [
            {"segment_id": s.id, "target_text": latest_draft[s.id].target_text,
             "notes": json.loads(latest_draft[s.id].notes_json or "[]"),
             "ambiguity": json.loads(latest_draft[s.id].ambiguity_json or "[]")}
            for s in window
        ]

    polished, carry = await translate_window_polish_direct(
        db_factory, project_id, window, draft, carry_over, model
    )
    errors = validate_window(
        [
            {"segment_id": s.id, "segment_no": s.segment_no, "segment_type": s.segment_type,
             "speaker_name": s.speaker_name, "source_text": s.source_text}
            for s in window
        ],
        polished,
    )
    if errors:
        return {"error": "; ".join(errors)}

    # context 供 QA 循环使用
    seg_dicts, context, _sg, _card = await _window_material(
        db_factory, project_id, window, carry_over
    )
    provider = get_provider()
    polished, carry, qa_issues = await _qa_check_loop(
        db_factory=db_factory,
        project_id=project_id,
        window=window,
        draft=draft,
        polished=polished,
        carry=carry,
        context=context,
        provider=provider,
        model=model,
    )

    with db_factory() as db:
        _persist_stage(db, window, polished, "polished", context, model)
        item = db.get(TaskItem, item_id)
        item.status = "succeeded"
        item.result_id = item.id
        item.finished_at = func.now()
        db.commit()
    # 任务级 progress 由调度层顺序写（并发 Polish 下避免读改写竞争）
    return {
        "done": 1,
        "window_id": window_id,
        "polish_carry": carry,
        "qa_issues": len(qa_issues),
    }


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
