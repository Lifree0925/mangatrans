"""批量翻译：窗口规划、任务展开、窗口执行、carry-over 串行传递、stale 管理。"""

import json
import re

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.llm.protocol import LLMError
from app.llm.registry import get_provider
from app.models.document import Chapter, SourceDocument, SourceSegment
from app.models.task import Task, TaskItem
from app.models.translation import StyleGuide, Translation
from app.services.context_builder import (
    ContextResult,
    build_context,
    build_project_bible,
    build_qa_reference,
)
from app.services import continuity_service
from app.services.context_cache import get_project_knowledge
from app.services.importer import sha256
from app.services.term_match import (
    build_speaker_map,
    build_term_records,
    canonicalize_exact_terms,
    forced_term_specs,
    is_address_form,
    normalize,
)
from app.services.title_service import preserved_title_for_segment
from app.services.translation import (
    PROMPT_VERSION, draft_prompt_block, localize_screenplay_marks,
    locate_problem_segments, repair_segments, translate_window,
    translate_window_draft, translate_window_polish, validate_window,
)
from app.services.window_planner import build_windows
from app.utils.token_budget import estimate_tokens


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

    chapter = db.get(Chapter, first.chapter_id) if first.chapter_id else None
    chapter_no = chapter.chapter_no if chapter is not None else 0
    states = (
        continuity_service.visible_character_states(db, project_id, chapter_no)
        if chapter_no > 0 else []
    )
    facts = (
        continuity_service.active_story_facts(db, project_id, chapter_no)
        if chapter_no > 0 else []
    )
    items = get_project_knowledge(db, project_id)
    return card, items, storyline, states, facts


def _approved_knowledge_entries(
    db: Session, project_id: str, *, item_types: tuple[str, ...] = ("term", "character")
) -> list[dict]:
    """一次读取项目知识及别名，避免逐术语 N+1 查询。"""
    return [
        item for item in get_project_knowledge(db, project_id)
        if item.get("item_type") in item_types
    ]


def _forced_terms_for_text(db: Session, project_id: str, text: str) -> dict[str, dict]:
    """文本命中的“精确执行术语”。

    只返回专名/头衔/具名实体；普通名词与情境词不会进入强制表。
    多词形重叠时最长匹配优先，歧义别名由 term_match 自动丢弃。
    """
    entries = _approved_knowledge_entries(db, project_id, item_types=("term",))
    records = build_term_records(entries)
    return forced_term_specs(records, text)


def _speaker_map_for_project(db: Session, project_id: str) -> dict[str, str]:
    """角色名 -> 英文正式名，用于译文说话人锚点校验。"""
    entries = _approved_knowledge_entries(db, project_id, item_types=("character",))
    return build_speaker_map(entries)


def _window_forced_terms(db: Session, project_id: str, window: list[SourceSegment]) -> dict[str, dict]:
    """窗口内命中的精确术语：{原文词形: {en, variants, ...}}。"""
    win_text = "".join(s.source_text for s in window)
    return _forced_terms_for_text(db, project_id, win_text)


async def process_window_qa_item(db_factory, task_id: str, item_id: str, model: str) -> dict:
    """独立 QA 节点：确定性 + 语义质检 → 最多 3 轮定点修复 → 自动质量门禁。

    QA 未完成、无法定位严重问题或修复 3 轮仍残留严重问题时，本节点失败，
    任务不得标记成功。修复只写问题段，不重启初翻/润色窗口。
    """
    from app.services import qa_service
    from app.services.translation import repair_segments

    with db_factory() as db:
        task = db.get(Task, task_id)
        item = db.get(TaskItem, item_id)
        if task is None or item is None:
            return {"error": "task/item not found"}
        seg_ids = json.loads(item.window_segments_json or "[]")
        window = list(db.scalars(select(SourceSegment).where(SourceSegment.id.in_(seg_ids))))
        window.sort(key=lambda s: seg_ids.index(s.id))
        project_id = task.project_id
        rows = list(db.scalars(
            select(Translation).where(
                Translation.segment_id.in_([s.id for s in window]),
                Translation.stage == "polished",
            ).order_by(Translation.version.desc())
        ))
        source_hashes = {s.id: s.source_hash for s in window}
        polished: dict[str, Translation] = {}
        for t in rows:
            if t.source_hash != source_hashes.get(t.segment_id):
                continue
            if t.freshness_status != "current":
                continue
            polished.setdefault(t.segment_id, t)
        draft_rows = list(db.scalars(
            select(Translation).where(
                Translation.segment_id.in_([s.id for s in window]),
                Translation.stage == "draft",
            ).order_by(Translation.version.desc())
        ))
        draft_latest: dict[str, Translation] = {}
        for t in draft_rows:
            draft_latest.setdefault(t.segment_id, t)
        forced = _window_forced_terms(db, project_id, window)
        speaker_map = _speaker_map_for_project(db, project_id)
        _card, knowledge, storyline, states, facts = _load_window_context(
            db, project_id, window
        )
        qa_reference = build_qa_reference(
            segments=[{
                "segment_id": s.id,
                "segment_type": s.segment_type,
                "speaker_name": s.speaker_name,
                "source_text": s.source_text,
            } for s in window],
            knowledge=knowledge,
            storyline=storyline,
            character_states=states,
            story_facts=facts,
        )

    if len(polished) != len(window):
        return {
            "error": "质检缺少当前润色译文，请重跑该窗口翻译",
            "error_code": "qa_missing_translation",
            "retryable": False,
        }

    seg_by_id = {s.id: s for s in window}
    source_block = "\n".join(f"[{s.id}] {s.source_text}" for s in window)
    target_block = "\n".join(f"[{s.id}] {polished[s.id].target_text}" for s in window)
    coverage_segments = [
        {"segment_id": s.id, "source_text": s.source_text} for s in window
    ]
    coverage_hint, coverage_stats = qa_service.build_coverage_signals(
        coverage_segments,
        {sid: tr.target_text for sid, tr in polished.items()},
    )
    semantic = await qa_service.semantic_qa(
        get_provider(), model, source_block, target_block,
        reference=qa_reference, coverage=coverage_hint,
    )
    if not semantic.ok:
        with db_factory() as db:
            qa_service.replace_window_quality(
                db, task_id, item.window_id or item.id, "blocked",
                {
                    "semantic_ok": False,
                    "semantic_error": semantic.error[:500],
                    "issues": 0,
                    "unresolved": 0,
                    "coverage": coverage_stats,
                },
            )
            db.commit()
        return {
            "error": f"语义质检未完成：{semantic.error or '未知错误'}",
            "error_code": "qa_incomplete",
            "retryable": False,
        }

    def _resolve(issue: dict) -> str:
        known = set(seg_by_id)
        sid = str(issue.get("segment_id") or "")
        if sid in known:
            return sid
        from app.services.term_match import normalize
        for field in ("source_quote", "target_quote"):
            quote = normalize(str(issue.get(field) or ""))
            if not quote:
                continue
            for seg in window:
                if quote and (quote in normalize(seg.source_text) or quote in normalize(polished[seg.id].target_text)):
                    return seg.id
        return ""

    semantic_by_seg: dict[str, list[dict]] = {}
    unlocated_severe = 0
    for issue in semantic.issues:
        sid = _resolve(issue)
        if sid:
            issue["segment_id"] = sid
            semantic_by_seg.setdefault(sid, []).append(issue)
        elif qa_service.should_repair(issue):
            unlocated_severe += 1

    det_total = 0
    naturalness_count = 0
    with db_factory() as db:
        forced = _window_forced_terms(db, project_id, window)
        # 独立 QA 在事实核对后给出的台词自然化修订：先过确定性护栏，
        # 再写新版本；随后统一跑 deterministic_qa，避免绕过术语/结构检查。
        accepted = qa_service.select_naturalness_edits(
            segments=[
                {
                    "segment_id": s.id,
                    "segment_type": s.segment_type,
                    "speaker_name": s.speaker_name,
                    "source_text": s.source_text,
                    "line_count_optional": bool(preserved_title_for_segment(s)),
                }
                for s in window
            ],
            targets={sid: tr.target_text for sid, tr in polished.items()},
            edits=semantic.naturalness_edits,
            forced_terms=forced,
            speaker_map=speaker_map,
            exclude_sids=set(semantic_by_seg),
        )
        for sid, row in persist_naturalness_edits(
            db, window=window, accepted=accepted, model=model
        ).items():
            polished[sid] = row
        naturalness_count = len(accepted)

        qa_service.clear_issues_for_segments(db, project_id, [s.id for s in window])
        for seg in window:
            det = qa_service.deterministic_qa(db, seg, polished[seg.id], forced)
            det_total += len(det)
            all_issues = det + semantic_by_seg.get(seg.id, [])
            if all_issues:
                qa_service.persist_issues(db, project_id, polished[seg.id].id, all_issues)
        db.commit()

    issues_total = unlocated_severe + det_total + sum(len(v) for v in semantic_by_seg.values())
    repaired_ids: set[str] = set()
    leftover: list[str] = []
    rounds = 0
    for round_no in range(1, 4):
        rounds = round_no
        with db_factory() as db:
            forced = _window_forced_terms(db, project_id, window)
            seg_fix: dict[str, list[dict]] = {}
            for seg in window:
                det = qa_service.deterministic_qa(db, seg, polished[seg.id], forced)
                need = [i for i in det if qa_service.should_repair(i)]
                if need:
                    seg_fix[seg.id] = need
            if round_no == 1:
                for sid, issues in semantic_by_seg.items():
                    need = [i for i in issues if qa_service.should_repair(i)]
                    if need:
                        seg_fix.setdefault(sid, []).extend(need)
        if not seg_fix:
            break
        target = sorted(seg_fix)
        target_window = [s for s in window if s.id in target]
        _sd, context, _sg, _card = await _window_material(db_factory, project_id, target_window, None)
        draft = [
            {"segment_id": s.id, "target_text": draft_latest[s.id].target_text}
            for s in window if s.id in target and s.id in draft_latest
        ]
        try:
            fixed_map = await repair_segments(
                provider=get_provider(), model=model, window_id=f"QA-{item.window_id}",
                context=context, draft=draft,
                issues=[i for lst in seg_fix.values() for i in lst],
                seg_ids=target,
            )
        except Exception:  # noqa: BLE001 修复失败：保留问题并阻断成功
            fixed_map = {}
        if not fixed_map:
            leftover = target
            break
        fixed_ok: dict[str, str] = {}
        for sid in target:
            text = (fixed_map.get(sid) or "").strip()
            seg = seg_by_id.get(sid)
            if not text or seg is None:
                continue
            if validate_window(
                [{
                    "segment_id": sid,
                    "segment_type": seg.segment_type,
                    "speaker_name": seg.speaker_name,
                    "source_text": seg.source_text,
                }],
                [{"segment_id": sid, "target_text": text}],
                speaker_map=speaker_map,
            ):
                continue
            fixed_ok[sid] = text
        if not fixed_ok:
            leftover = target
            break
        with db_factory() as db:
            for sid, text in fixed_ok.items():
                insert_translation_row(
                    db, segment_id=sid, stage="polished", target_text=text,
                    review_status="unreviewed", freshness_status="current",
                    notes_json="[]", ambiguity_json="[]",
                    model_name=model, provider_id="default",
                    prompt_version=PROMPT_VERSION,
                    context_snapshot_json=json.dumps(context.snapshot, ensure_ascii=False),
                    source_hash=sha256(seg_by_id[sid].source_text),
                )
                polished[sid].target_text = text
            db.commit()
        repaired_ids.update(fixed_ok)
        still_bad: list[str] = []
        with db_factory() as db:
            forced = _window_forced_terms(db, project_id, window)
            for sid in fixed_ok:
                det = qa_service.deterministic_qa(db, seg_by_id[sid], polished[sid], forced)
                if any(qa_service.should_repair(i) for i in det):
                    still_bad.append(sid)
        leftover = still_bad
        if not leftover:
            break

    # 语义问题被修复后，再对修复段做一次聚焦语义复检；避免“改过但没人再看”。
    semantic_after_ok = True
    coverage_after_stats = coverage_stats
    if repaired_ids and semantic_by_seg:
        with db_factory() as db:
            latest = {
                t.segment_id: t for t in db.scalars(
                    select(Translation).where(
                        Translation.segment_id.in_(sorted(repaired_ids)),
                        Translation.stage == "polished",
                    ).order_by(Translation.version.desc())
                )
            }
            target_segs = [s for s in window if s.id in repaired_ids]
            source_block = "\n".join(f"[{s.id}] {s.source_text}" for s in target_segs)
            target_block = "\n".join(
                f"[{s.id}] {latest[s.id].target_text}" for s in target_segs if s.id in latest
            )
        if source_block and target_block:
            coverage_after_hint, coverage_after_stats = qa_service.build_coverage_signals(
                [
                    {"segment_id": s.id, "source_text": s.source_text}
                    for s in target_segs
                ],
                {
                    s.id: polished[s.id].target_text
                    for s in target_segs if s.id in polished
                },
            )
            recheck = await qa_service.semantic_qa(
                get_provider(), model, source_block, target_block,
                reference=qa_reference, coverage=coverage_after_hint,
            )
            if not recheck.ok:
                semantic_after_ok = False
            else:
                for issue in recheck.issues:
                    sid = str(issue.get("segment_id") or "")
                    if sid not in repaired_ids:
                        continue
                    issue["segment_id"] = sid
                    if qa_service.should_repair(issue):
                        leftover.append(sid)
                        with db_factory() as db:
                            row = db.scalar(
                                select(Translation).where(
                                    Translation.segment_id == sid,
                                    Translation.stage == "polished",
                                ).order_by(Translation.version.desc()).limit(1)
                            )
                            if row is not None:
                                qa_service.persist_issues(db, project_id, row.id, [issue])
            leftover = sorted(set(leftover))

    gate = "blocked" if (not semantic_after_ok or unlocated_severe) else (
        "needs_review" if leftover else "passed"
    )
    with db_factory() as db:
        latest_rows = list(db.scalars(
            select(Translation).where(
                Translation.segment_id.in_([s.id for s in window]),
                Translation.stage == "polished",
            ).order_by(Translation.version.desc())
        ))
        latest_ids: list[str] = []
        seen: set[str] = set()
        for row in latest_rows:
            if row.segment_id not in seen:
                seen.add(row.segment_id)
                latest_ids.append(row.id)
        qa_service.replace_window_quality(
            db, task_id, item.window_id or item.id, gate,
            {
                "semantic_ok": semantic_after_ok,
                "semantic_error": "" if semantic_after_ok else "语义复检未完成",
                "issues": issues_total,
                "unresolved": len(leftover) + unlocated_severe,
                "rounds": rounds,
                "translation_ids": latest_ids,
                "coverage": coverage_after_stats,
                "naturalness_edits": naturalness_count,
            },
        )
        db.commit()

    if gate != "passed":
        return {
            "error": (
                "自动质检未通过：语义质检未完成"
                if not semantic_after_ok
                else f"自动质检未通过：{len(leftover) + unlocated_severe} 个严重问题未解决"
            ),
            "error_code": "quality_gate",
            "retryable": False,
            "qa_issues": issues_total,
            "gate": gate,
        }

    with db_factory() as db:
        item = db.get(TaskItem, item_id)
        if item is not None:
            item.status = "succeeded"
            item.finished_at = func.now()
            db.commit()
    return {
        "done": 1,
        "window_id": item.window_id if item else None,
        "qa_issues": issues_total,
        "repaired": len(repaired_ids),
        "gate": gate,
    }

def insert_translation_row(db: Session, *, segment_id: str, **kw) -> Translation:
    """并发安全的译文插入。

    版本号由同一条 INSERT ... SELECT COALESCE(MAX(version),0)+1 原子分配：
    SQLite 单语句（隐式事务）内完成读 max 与插入，跨协程/跨进程都不会再产生
    UNIQUE(segment_id, version) 冲突（历史根因：应用层"读 max+1"存在竞态窗口）。
    保留 busy/locked 与残余完整性冲突的兜底重试。
    """
    # raw SQL 不触发 ORM 的 Python 端 default：NOT NULL 列统一显式补默认
    kw.setdefault("review_status", "unreviewed")
    kw.setdefault("freshness_status", "current")
    kw.setdefault("notes_json", "[]")
    kw.setdefault("ambiguity_json", "[]")
    kw.setdefault("model_name", "")
    kw.setdefault("provider_id", "")
    kw.setdefault("prompt_version", "")
    kw.setdefault("context_snapshot_json", "{}")
    kw.setdefault("source_hash", "")
    last_exc: Exception | None = None
    from app.models.translation import new_id as _new_id, utcnow as _utcnow

    kw.setdefault("created_at", _utcnow())

    for _ in range(4):
        try:
            db.commit()  # 隔离本次失败影响面：此前已 flush 的行先落库
            rid = _new_id()
            cols = ["id", "segment_id", "version", *kw.keys()]
            select_exprs = [
                ":id",
                ":segment_id",
                "COALESCE(MAX(version),0)+1",
                *(f":{c}" for c in kw),
            ]
            sql = (
                "INSERT INTO translations ("
                + ", ".join(cols)
                + ") SELECT "
                + ", ".join(select_exprs)
                + " FROM translations WHERE segment_id = :segment_id"
            )
            db.execute(text(sql), {"id": rid, "segment_id": segment_id, **kw})
            db.commit()
            t = db.get(Translation, rid)
            if t is None:  # 理论不可达：插入后按主键读取必须存在
                raise RuntimeError("译文插入后读取失败")
            return t
        except (IntegrityError, OperationalError) as exc:
            last_exc = exc
            db.rollback()  # 只回滚当前这一行
            continue
    assert last_exc is not None
    raise last_exc


def persist_naturalness_edits(
    db: Session, *, window: list[SourceSegment], accepted: dict[str, str], model: str,
) -> dict[str, Translation]:
    """把 QA 已通过护栏的台词自然化文本写成新的 polished 版本。"""
    if not accepted:
        return {}
    seg_by_id = {s.id: s for s in window}
    rows: dict[str, Translation] = {}
    for sid, text in accepted.items():
        seg = seg_by_id.get(sid)
        if seg is None or not str(text or "").strip():
            continue
        rows[sid] = insert_translation_row(
            db,
            segment_id=sid,
            stage="polished",
            target_text=text.strip(),
            review_status="unreviewed",
            freshness_status="current",
            notes_json=json.dumps(["qa-naturalness-edit"], ensure_ascii=False),
            ambiguity_json="[]",
            model_name=model,
            provider_id="default",
            prompt_version=PROMPT_VERSION,
            context_snapshot_json=json.dumps(
                {"qa_naturalness_edit": True}, ensure_ascii=False
            ),
            source_hash=sha256(seg.source_text),
        )
    return rows


def _persist_stage(
    db: Session, window: list[SourceSegment], entries: list[dict], stage: str,
    context, model: str, *, only_segment_ids: set[str] | None = None,
    review_status: str | None = None,
) -> None:
    """按阶段入库（draft 与 polished 分别持久化）。

    按 segment_id 映射回填（不按下标 zip：输出顺序错位时绝不写错段）；
    only_segment_ids 只落库指定段（QA 定点修复只给修复段追加新版本，防版本爆炸）。
    """
    by_id = {e.get("segment_id"): e for e in entries}
    for src in window:
        if only_segment_ids is not None and src.id not in only_segment_ids:
            continue
        entry = by_id.get(src.id)
        if entry is None:
            continue  # 缺失段不入库（调用方必须先过 validate_window）
        target_text = entry["target_text"]
        notes = list(entry.get("notes", []))
        # 开头已带英文标题的片段：确定性保留原文英文标题，不采用模型改写结果。
        preserved_title = preserved_title_for_segment(src)
        if preserved_title:
            target_text = preserved_title
            notes.append("source-title-preserved")
        kw = dict(
            stage=stage,
            target_text=target_text,
            notes_json=json.dumps(notes, ensure_ascii=False),
            ambiguity_json=json.dumps(entry.get("ambiguity", []), ensure_ascii=False),
            model_name=model,
            provider_id="default",
            prompt_version=PROMPT_VERSION,
            context_snapshot_json=json.dumps(context.snapshot, ensure_ascii=False),
            source_hash=sha256(src.source_text),
        )
        if review_status:
            kw["review_status"] = review_status
        insert_translation_row(db, segment_id=src.id, **kw)


def _persist_window_translations(
    db: Session, window: list[SourceSegment], draft, polished, context, model: str
) -> None:
    """draft 与 polished 入库：按 segment_id 映射回填，顺序无关、绝不写错段。"""
    draft_by_id = {e.get("segment_id"): e for e in draft}
    polished_by_id = {e.get("segment_id"): e for e in polished}
    for src in window:
        for stage, by_id in (("draft", draft_by_id), ("polished", polished_by_id)):
            entry = by_id.get(src.id)
            if entry is None:
                continue  # 缺失段不入库（调用方必须先过 validate_window）
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


async def _window_material(
    db_factory, project_id: str, window: list[SourceSegment], carry_over: dict | None,
    *, reserved_tokens: int = 0, lean: bool = False, draft_only: bool = False,
) -> tuple[list[dict], ContextResult, dict | None, dict | None]:
    """窗口公共材料：segment dicts、style guide、场景卡/知识/剧情卡、context。"""
    with db_factory() as db:
        _card, knowledge, storyline, states, facts = _load_window_context(
            db, project_id, window
        )
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
            "line_count_optional": bool(preserved_title_for_segment(s)),
        }
        for s in window
    ]
    # Lean 是上下文撑满后的保命路径，不再塞完整项目前缀；正常 Draft/Polish
    # 使用逐字稳定的项目前缀，才能让 Provider 缓存跨窗口复用。
    project_bible = "" if lean else build_project_bible(
        knowledge, None if draft_only else style_guide
    )
    context_style = None if draft_only else style_guide
    context = build_context(
        segments=seg_dicts,
        scene_card=_card,
        knowledge=knowledge,
        style_guide=context_style,
        carry_over=carry_over,
        storyline=storyline,
        character_states=states,
        story_facts=facts,
        project_bible=project_bible,
        reserved_tokens=reserved_tokens,
        lean=lean,
        draft_only=draft_only,
    )
    return seg_dicts, context, style_guide, _card


def _merge_split_contexts(parts: list[ContextResult]) -> ContextResult:
    """合并拆分窗口的上下文快照，保证译文可追溯。"""
    if len(parts) == 1:
        return parts[0]
    snapshot: dict = {"knowledge_ids": [], "scene_card_id": None, "carry_over_hash": None}
    for ctx in parts:
        for key, value in (ctx.snapshot or {}).items():
            if key == "knowledge_ids" and isinstance(value, list):
                snapshot[key] = list(dict.fromkeys([*(snapshot.get(key) or []), *value]))
            elif snapshot.get(key) in (None, "", [], {}):
                snapshot[key] = value
    return ContextResult(
        prompt_text="\n\n".join(c.prompt_text for c in parts),
        report={"split_window": len(parts)},
        snapshot=snapshot,
        total_tokens=sum(c.total_tokens for c in parts),
    )


_WINDOW_SPLIT_CODES = {"output_truncated", "context_length"}


_HONORIFIC_RE = re.compile(
    r"\b(Mr|Mrs|Ms|Miss|Madam)\.?\s+([A-Z][A-Za-z'’-]*)\b"
)


def _keep_possessive(original: str, replacement: str) -> str:
    if original.endswith("’s"):
        return replacement + "’s"
    if original.endswith("'s"):
        return replacement + "'s"
    return replacement


def apply_known_honorifics(text: str, family_by_given: dict[str, str]) -> str:
    """将 Mr./Mrs. + 名字改为姓氏；家庭姓氏只来自项目人物知识。"""
    if not text or not family_by_given:
        return text

    def repl(match: re.Match) -> str:
        family = family_by_given.get(match.group(2).casefold())
        if not family:
            return match.group(0)
        raw_title = match.group(1)
        if raw_title.casefold() == "madam":
            return _keep_possessive(match.group(0), f"Mrs. {family}")
        if raw_title.casefold() == "miss":
            return _keep_possessive(match.group(0), f"Miss {family}")
        return _keep_possessive(match.group(0), f"{raw_title}. {family}")

    return _HONORIFIC_RE.sub(repl, text)


_ADDRESS_SOURCE_RE = re.compile(r"([A-Za-z][A-Za-z'’-]{1,20})(先生|夫人|小姐|女士)")
_ADDRESS_TITLES = {"先生": "Mr.", "夫人": "Mrs.", "小姐": "Miss", "女士": "Ms."}
_ELDER_SOURCE_RE = re.compile(r"([A-Z][A-Za-z'’-]+)老先生")
_TARGET_ADDRESS_RE = re.compile(
    r"\b(Mr|Mrs|Ms|Miss|Madam)\.?\s+([A-Z][A-Za-z'’-]*)\b"
)


def apply_source_honorifics(
    text: str, source_text: str, family_by_given: dict[str, str],
    approved_addresses: dict[str, str] | None = None,
) -> str:
    """按原文称谓顺序修正目标中的头衔组合。

    例：源文「Cross先生，Damian先生」目标「Mr. Cross, Mr. Damian」
    → 「Mr. Cross, Mr. Sterling」。同句多人时按出现顺序对齐，不能按姓氏全局替换。
    """
    if not text or not source_text:
        return text
    # “Sterling老先生”不是 Mr. + 姓氏的普通称谓，而是“老/先辈”限定；
    # 保持这一限定，避免和 Damian 的 Mr. Sterling 混淆。
    for elder in _ELDER_SOURCE_RE.finditer(source_text):
        family = elder.group(1)
        pattern = re.compile(rf"\bMr\.?\s+{re.escape(family)}\b")
        if not re.search(rf"\bthe\s+elder\s+Mr\.?\s+{re.escape(family)}\b", text, re.I):
            text = pattern.sub(f"the elder Mr. {family}", text, count=1)
    addresses = list(_ADDRESS_SOURCE_RE.finditer(source_text))
    targets = list(_TARGET_ADDRESS_RE.finditer(text))
    if not addresses or not targets:
        return text
    known_families = {str(v).casefold() for v in family_by_given.values() if v}
    expected: list[str | None] = []
    for match in addresses:
        given, suffix = match.group(1), match.group(2)
        title = _ADDRESS_TITLES.get(suffix, "Mr.")
        approved = (approved_addresses or {}).get(normalize(match.group(0)))
        if approved:
            expected.append(approved)
            continue
        family = family_by_given.get(given.casefold())
        if family:
            expected.append(f"{title} {family}")
        elif given.casefold() in known_families:
            expected.append(f"{title} {given}")
        else:
            expected.append(None)

    if len(targets) == len(expected):
        # 同句多人：按顺序一一替换；未知项的原文称谓保持不动。
        out: list[str] = []
        cursor = 0
        for match, wanted in zip(targets, expected):
            out.append(text[cursor:match.start()])
            out.append(_keep_possessive(match.group(0), wanted) if wanted else match.group(0))
            cursor = match.end()
        out.append(text[cursor:])
        return "".join(out)

    if len(addresses) != 1 or expected[0] is None:
        return text

    # 单人且目标只有一处：允许修正错误姓氏（如 Helen 被写成 Mrs. Vance）。
    # 目标有多处时，不改可能属于其他人的称谓，交给上面的顺序对齐或修复层。
    if len(targets) == 1:
        match = targets[0]
        return text[:match.start()] + _keep_possessive(match.group(0), expected[0]) + text[match.end():]
    return text


def _honorific_family_map(db, project_id: str) -> dict[str, str]:
    """从人物知识推出称呼姓氏；只做人名与直系关系可证实的映射。"""
    items = get_project_knowledge(db, project_id)
    chars = [x for x in items if x.get("item_type") == "character"]
    full: list[tuple[str, str]] = []
    for ch in chars:
        data = ch.get("data") if isinstance(ch.get("data"), dict) else {}
        name = str(data.get("translation") or ch.get("canonical_key") or "").strip()
        tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", name)
        if len(tokens) >= 2:
            full.append((tokens[0], tokens[-1]))
    mapping = {given.casefold(): family for given, family in full}
    # Helen（Damian 的母亲）这类单名人物：从显式直系关系推导家族姓氏。
    for ch in chars:
        data = ch.get("data") if isinstance(ch.get("data"), dict) else {}
        name = str(data.get("translation") or ch.get("canonical_key") or "").strip()
        tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", name)
        if len(tokens) != 1:
            continue
        desc = " ".join(
            str(data.get(k) or "")
            for k in ("role", "role_translation", "relationships")
        )
        # 只从显式的「X 的母亲/父亲/妻子/丈夫」取 X 的姓氏；
        # 不能因为在关系描述里出现了孙辈 Leo 就误推成 Vance。
        relation = re.search(
            r"\b([A-Z][A-Za-z'’-]*)['’]s\s+(mother|father|wife|husband)\b",
            desc,
            re.I,
        )
        if relation:
            given = relation.group(1)
        else:
            relation = re.search(
                r"\b(mother|father|wife|husband)\s+of\s+([A-Z][A-Za-z'’-]*)\b",
                desc,
                re.I,
            )
            given = relation.group(2) if relation else ""
        if not given:
            continue
        family = dict(full).get(given)
        if family:
            mapping.setdefault(tokens[0].casefold(), family)
    return mapping


def _approved_address_forms(db, project_id: str) -> dict[str, str]:
    """读取知识库中已批准的有头衔称谓，如 Helen夫人 -> Madam Helen。

    exact 术语表不收录称谓，但称谓也需要项目级唯一译名。这里只接受目标值
    本身带 Mr./Mrs./Miss/Madam 的形式，避免把普通人名误当成称谓。
    """
    anchors = re.compile(r"^(?:Mr|Mrs|Ms|Miss|Madam)\.?\s+", re.I)
    out: dict[str, str] = {}
    for item in get_project_knowledge(db, project_id):
        if item.get("item_type") != "character":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        target = str(data.get("translation") or "").strip()
        if not anchors.match(target):
            continue
        forms = [item.get("canonical_key", ""), *(item.get("aliases") or [])]
        forms.extend(data.get("forms_of_address") or [])
        for form in forms:
            form = str(form or "").strip()
            if form and is_address_form(form):
                out.setdefault(normalize(form), target)
    return out


async def _canonicalize_polished_terms(db_factory, project_id, window, polished):
    """专名大小写确定性归一（只改 exact 术语，不碰普通名词/事实/风格）。"""
    with db_factory() as db:
        forced = _window_forced_terms(db, project_id, window)
        honorifics = _honorific_family_map(db, project_id)
        approved_addresses = _approved_address_forms(db, project_id)
    for entry in polished:
        text = entry.get("target_text") or ""
        if forced:
            text = canonicalize_exact_terms(text, forced)
        text = localize_screenplay_marks(text)
        source_text = next((s.source_text for s in window if s.id == entry.get("segment_id")), "")
        text = apply_known_honorifics(text, honorifics)
        entry["target_text"] = apply_source_honorifics(
            text, source_text, honorifics, approved_addresses
        )
    return polished


def _minimal_context(seg_dicts: list[dict]) -> ContextResult:
    """上下文超限时的单段兜底：只保留原文，不带知识库/剧情/carry。"""
    return build_context(
        segments=seg_dicts,
        scene_card=None,
        knowledge=[],
        style_guide=None,
        carry_over=None,
        budget=max(1200, estimate_tokens(seg_dicts[0].get("source_text", "")) + 400),
    )


async def translate_window_draft_direct(
    db_factory, project_id: str, window: list[SourceSegment], model: str,
    *, _depth: int = 0, task_id: str | None = None, window_key: str | None = None,
) -> tuple[list[dict], dict | None, ContextResult]:
    """Draft 阶段（可并发）；截断/上下文超限时自动拆半，避免整窗永久卡死。"""
    try:
        seg_dicts, context, _sg, _card = await _window_material(
            db_factory, project_id, window, None, draft_only=True
        )
        draft, carry = await translate_window_draft(
            provider=get_provider(), model=model, window_id="DRAFT", context=context,
            source_chars=sum(len(s["source_text"]) for s in seg_dicts),
            segments=seg_dicts,
            task_id=task_id, window_key=window_key,
        )
        return draft, carry, context
    except LLMError as exc:
        code = getattr(exc, "code", "")
        if code not in _WINDOW_SPLIT_CODES:
            raise
        if len(window) <= 1 and code == "context_length":
            seg_dicts = [
                {
                    "segment_id": s.id,
                    "segment_no": s.segment_no,
                    "segment_type": s.segment_type,
                    "speaker_name": s.speaker_name,
                    "source_text": s.source_text,
                    "line_count_optional": bool(preserved_title_for_segment(s)),
                }
                for s in window
            ]
            context = _minimal_context(seg_dicts)
            draft, carry = await translate_window_draft(
                provider=get_provider(), model=model, window_id="DRAFT-MIN",
                context=context,
                source_chars=sum(len(s["source_text"]) for s in seg_dicts),
                segments=seg_dicts,
            )
            return draft, carry, context
        if len(window) <= 1 or _depth >= 3:
            raise
        mid = len(window) // 2
        left, _left_carry, ctx_a = await translate_window_draft_direct(
            db_factory, project_id, window[:mid], model, _depth=_depth + 1,
            task_id=task_id, window_key=window_key,
        )
        right, carry, ctx_b = await translate_window_draft_direct(
            db_factory, project_id, window[mid:], model, _depth=_depth + 1,
            task_id=task_id, window_key=window_key,
        )
        return [*left, *right], carry, _merge_split_contexts([ctx_a, ctx_b])


async def translate_window_polish_direct(
    db_factory, project_id: str, window: list[SourceSegment], draft: list[dict],
    carry_over: dict | None, model: str, *, _depth: int = 0,
    task_id: str | None = None, window_key: str | None = None,
) -> tuple[list[dict], dict]:
    """Polish 阶段；上下文撑满时先精简知识重试，再拆半，最后单段极简兜底。"""

    async def _polish_once(*, lean: bool, force_no_thinking: bool = False):
        # Draft 块在 context.prompt_text 之外追加，构建上下文时必须先预留其预算。
        reserved = estimate_tokens(draft_prompt_block(draft))
        seg_dicts, context, _sg, _card = await _window_material(
            db_factory, project_id, window, carry_over,
            reserved_tokens=reserved, lean=lean,
        )
        polished, carry = await translate_window_polish(
            provider=get_provider(), model=model,
            window_id="POLISH-LEAN" if lean else "POLISH",
            context=context, draft=draft, carry_over=carry_over, segments=seg_dicts,
            force_no_thinking=force_no_thinking,
            task_id=None if lean else task_id,
            window_key=None if lean else window_key,
        )
        return await _canonicalize_polished_terms(
            db_factory, project_id, window, polished
        ), carry

    try:
        return await _polish_once(lean=False)
    except LLMError as exc:
        code = getattr(exc, "code", "")
        if code not in _WINDOW_SPLIT_CODES:
            raise
        # 第一次撑满：不立刻拆窗，先清掉当前窗口未命中的知识再试一次。
        try:
            return await _polish_once(lean=True, force_no_thinking=True)
        except LLMError as lean_exc:
            exc = lean_exc
            code = getattr(exc, "code", "")
            if code not in _WINDOW_SPLIT_CODES:
                raise
        if len(window) <= 1:
            # 单段窗口不能再拆：去掉知识库/剧情/carry 并关闭思考，优先保证完成。
            if code != "context_length":
                raise
            seg_dicts = [
                {
                    "segment_id": s.id,
                    "segment_no": s.segment_no,
                    "segment_type": s.segment_type,
                    "speaker_name": s.speaker_name,
                    "source_text": s.source_text,
                    "line_count_optional": bool(preserved_title_for_segment(s)),
                }
                for s in window
            ]
            context = _minimal_context(seg_dicts)
            try:
                polished, carry = await translate_window_polish(
                    provider=get_provider(), model=model, window_id="POLISH-MIN",
                    context=context, draft=draft, carry_over=None, segments=seg_dicts,
                    force_no_thinking=True,
                )
            except LLMError as retry_exc:
                if getattr(retry_exc, "code", "") == "context_length":
                    raise LLMError(
                        "最后窗口仅剩单段仍超出模型上下文；请缩短该段原文或更换更大上下文的模型",
                        retryable=False,
                        code="context_length_single_segment",
                    ) from retry_exc
                raise
            return (
                await _canonicalize_polished_terms(db_factory, project_id, window, polished),
                carry,
            )
        if _depth >= 3:
            raise
        mid = len(window) // 2
        left_window, right_window = window[:mid], window[mid:]
        left_ids = {s.id for s in left_window}
        left_draft = [e for e in draft if e.get("segment_id") in left_ids]
        right_draft = [e for e in draft if e.get("segment_id") not in left_ids]
        left, middle_carry = await translate_window_polish_direct(
            db_factory, project_id, left_window, left_draft, carry_over, model,
            _depth=_depth + 1, task_id=task_id, window_key=window_key,
        )
        right, carry = await translate_window_polish_direct(
            db_factory, project_id, right_window, right_draft, middle_carry, model,
            _depth=_depth + 1, task_id=task_id, window_key=window_key,
        )
        return [*left, *right], carry


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
        speaker_map = _speaker_map_for_project(db, project_id)

    draft, carry, context = await translate_window_draft_direct(
        db_factory, project_id, window, model,
        task_id=task_id, window_key=item.window_id,
    )
    errors = validate_window(
        [
            {
                "segment_id": s.id,
                "segment_type": s.segment_type,
                "speaker_name": s.speaker_name,
                "source_text": s.source_text,
                "line_count_optional": bool(preserved_title_for_segment(s)),
            }
            for s in window
        ],
        draft,
        speaker_map=speaker_map,
    )
    if errors:
        # 段缺失/顺序错位/重复：拒绝按序回填（历史串段根因），整窗重试
        return {"error": "draft 校验失败：" + "; ".join(errors[:3])}
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
        speaker_map = _speaker_map_for_project(db, project_id)
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
        source_hashes = {s.id: s.source_hash for s in window}
        for t in draft_rows:
            if t.source_hash != source_hashes.get(t.segment_id):
                continue
            if t.freshness_status != "current":
                continue
            latest_draft.setdefault(t.segment_id, t)
        if len(latest_draft) != len(window):
            return {
                "error": "draft 缺失，窗口需重新执行 draft 阶段",
                "error_code": "draft_missing",
                "retryable": False,
            }
        draft = [
            {"segment_id": s.id, "target_text": latest_draft[s.id].target_text,
             "notes": json.loads(latest_draft[s.id].notes_json or "[]"),
             "ambiguity": json.loads(latest_draft[s.id].ambiguity_json or "[]")}
            for s in window
        ]

    polished, carry = await translate_window_polish_direct(
        db_factory, project_id, window, draft, carry_over, model,
        task_id=task_id, window_key=window_id,
    )
    seg_dicts = [
        {"segment_id": s.id, "segment_no": s.segment_no, "segment_type": s.segment_type,
         "speaker_name": s.speaker_name, "source_text": s.source_text,
         "line_count_optional": bool(preserved_title_for_segment(s))}
        for s in window
    ]
    errors = validate_window(seg_dicts, polished, speaker_map=speaker_map)
    if errors:
        # 与 Express 一致：只重译问题段，不整窗重做；修复后仍不合格才失败。
        bad_ids = locate_problem_segments(seg_dicts, polished, speaker_map=speaker_map)
        if bad_ids:
            try:
                _sd, repair_ctx, _sg, _card = await _window_material(
                    db_factory, project_id, window, carry_over,
                    reserved_tokens=estimate_tokens(draft_prompt_block(draft)),
                )
                fix_issues = [
                    {"issue_type": "line_shift", "explanation": e,
                     "suggested_fix": "补齐缺失行，逐行对应原文"}
                    for e in errors
                ]
                fixed_map = await repair_segments(
                    provider=get_provider(), model=model,
                    window_id=f"BATCH-FIX-{window[0].segment_no}",
                    context=repair_ctx, draft=draft, issues=fix_issues,
                    seg_ids=sorted(bad_ids),
                )
                for entry in polished:
                    if entry.get("segment_id") in fixed_map:
                        entry["target_text"] = fixed_map[entry["segment_id"]]
                errors = validate_window(seg_dicts, polished, speaker_map=speaker_map)
            except Exception:  # noqa: BLE001 修复失败保留原校验错误
                pass
    if errors:
        return {
            "error": "; ".join(errors),
            "error_code": "window_validation",
            "retryable": False,
        }

    # polished 整窗先落库一次（QA 定点修复由独立 QA 节点追加新版本）
    seg_dicts, context, _sg, _card = await _window_material(
        db_factory, project_id, window, carry_over,
        reserved_tokens=estimate_tokens(draft_prompt_block(draft)),
    )
    with db_factory() as db:
        _persist_stage(db, window, polished, "polished", context, model)
        db.commit()

    # 创建独立 QA 节点：QA 打回绝不重启本窗口的初翻/润色，
    # QA 节点失败/超时只重跑自身（定点查询+定点修改）。
    with db_factory() as db:
        exists = db.scalar(
            select(TaskItem.id).where(
                TaskItem.task_id == task_id,
                TaskItem.phase == "qa",
                TaskItem.window_id == item.window_id,
            ).limit(1)
        )
        if exists is None:
            db.add(TaskItem(
                task_id=task_id,
                segment_id=window[0].id,
                window_id=item.window_id,
                phase="qa",
                window_segments_json=item.window_segments_json,
            ))
        item = db.get(TaskItem, item_id)
        item.status = "succeeded"
        item.result_id = item.id
        item.finished_at = func.now()
        db.commit()
    # 任务级 progress 由调度层顺序写（并发 Polish 下避免读改写竞争）
    return {"done": 1, "window_id": window_id, "polish_carry": carry}


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
