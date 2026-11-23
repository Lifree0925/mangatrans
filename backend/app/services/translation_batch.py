"""批量翻译：窗口规划、任务展开、窗口执行、carry-over 串行传递、stale 管理。"""

import json

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.llm.protocol import LLMError
from app.llm.registry import get_provider
from app.models.document import SourceDocument, SourceSegment
from app.models.task import Task, TaskItem
from app.models.translation import StyleGuide, Translation
from app.services.context_builder import ContextResult, build_context, build_qa_reference
from app.services.context_cache import get_project_knowledge
from app.services.importer import sha256
from app.services.term_match import (
    build_speaker_map,
    build_term_records,
    canonicalize_exact_terms,
    forced_term_specs,
)
from app.services.title_service import preserved_title_for_segment
from app.services.translation import (
    PROMPT_VERSION, localize_screenplay_marks, translate_window,
    translate_window_draft, translate_window_polish, validate_window,
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

    items = get_project_knowledge(db, project_id)
    return card, items, storyline


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
        _card, knowledge, storyline = _load_window_context(db, project_id, window)
        qa_reference = build_qa_reference(
            segments=[{
                "segment_id": s.id,
                "segment_type": s.segment_type,
                "speaker_name": s.speaker_name,
                "source_text": s.source_text,
            } for s in window],
            knowledge=knowledge,
            storyline=storyline,
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
    semantic = await qa_service.semantic_qa(
        get_provider(), model, source_block, target_block, reference=qa_reference
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
    with db_factory() as db:
        qa_service.clear_issues_for_segments(db, project_id, [s.id for s in window])
        forced = _window_forced_terms(db, project_id, window)
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
            recheck = await qa_service.semantic_qa(
                get_provider(), model, source_block, target_block, reference=qa_reference
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
            "line_count_optional": bool(preserved_title_for_segment(s)),
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


async def translate_window_draft_direct(
    db_factory, project_id: str, window: list[SourceSegment], model: str,
    *, _depth: int = 0,
) -> tuple[list[dict], dict | None, ContextResult]:
    """Draft 阶段（可并发）；输出截断时自动拆半重跑，避免整窗永久卡死。"""
    try:
        seg_dicts, context, _sg, _card = await _window_material(db_factory, project_id, window, None)
        draft, carry = await translate_window_draft(
            provider=get_provider(), model=model, window_id="DRAFT", context=context,
            source_chars=sum(len(s["source_text"]) for s in seg_dicts),
        )
        return draft, carry, context
    except LLMError as exc:
        if getattr(exc, "code", "") != "output_truncated" or len(window) <= 1 or _depth >= 3:
            raise
        mid = len(window) // 2
        left, _left_carry, ctx_a = await translate_window_draft_direct(
            db_factory, project_id, window[:mid], model, _depth=_depth + 1
        )
        right, carry, ctx_b = await translate_window_draft_direct(
            db_factory, project_id, window[mid:], model, _depth=_depth + 1
        )
        return [*left, *right], carry, _merge_split_contexts([ctx_a, ctx_b])


async def translate_window_polish_direct(
    db_factory, project_id: str, window: list[SourceSegment], draft: list[dict],
    carry_over: dict | None, model: str, *, _depth: int = 0,
) -> tuple[list[dict], dict]:
    """Polish 阶段；输出截断时自动拆半并把左半 carry 传给右半。"""
    try:
        seg_dicts, context, _sg, _card = await _window_material(
            db_factory, project_id, window, carry_over
        )
        polished, carry = await translate_window_polish(
            provider=get_provider(), model=model, window_id="POLISH",
            context=context, draft=draft, carry_over=carry_over, segments=seg_dicts,
        )
        # 专名大小写确定性归一（只改 exact 术语，不碰普通名词/事实/风格）。
        with db_factory() as db:
            forced = _window_forced_terms(db, project_id, window)
        for entry in polished:
            text = entry.get("target_text") or ""
            if forced:
                text = canonicalize_exact_terms(text, forced)
            entry["target_text"] = localize_screenplay_marks(text)
        return polished, carry
    except LLMError as exc:
        if getattr(exc, "code", "") != "output_truncated" or len(window) <= 1 or _depth >= 3:
            raise
        mid = len(window) // 2
        left_window, right_window = window[:mid], window[mid:]
        left_ids = {s.id for s in left_window}
        left_draft = [e for e in draft if e.get("segment_id") in left_ids]
        right_draft = [e for e in draft if e.get("segment_id") not in left_ids]
        left, middle_carry = await translate_window_polish_direct(
            db_factory, project_id, left_window, left_draft, carry_over, model,
            _depth=_depth + 1,
        )
        right, carry = await translate_window_polish_direct(
            db_factory, project_id, right_window, right_draft, middle_carry, model,
            _depth=_depth + 1,
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

    draft, carry, context = await translate_window_draft_direct(db_factory, project_id, window, model)
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
        db_factory, project_id, window, draft, carry_over, model
    )
    errors = validate_window(
        [
            {"segment_id": s.id, "segment_no": s.segment_no, "segment_type": s.segment_type,
             "speaker_name": s.speaker_name, "source_text": s.source_text,
             "line_count_optional": bool(preserved_title_for_segment(s))}
            for s in window
        ],
        polished,
        speaker_map=speaker_map,
    )
    if errors:
        return {
            "error": "; ".join(errors),
            "error_code": "window_validation",
            "retryable": False,
        }

    # polished 整窗先落库一次（QA 定点修复由独立 QA 节点追加新版本）
    seg_dicts, context, _sg, _card = await _window_material(
        db_factory, project_id, window, carry_over
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
