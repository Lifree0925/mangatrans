"""批量翻译：窗口规划、任务展开、窗口执行、carry-over 串行传递、stale 管理。"""

import json

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.llm.registry import get_provider
from app.models.document import SourceDocument, SourceSegment
from app.models.knowledge import KnowledgeAlias, KnowledgeItem
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


def _forced_terms_for_text(db: Session, project_id: str, text: str) -> dict[str, dict]:
    """文本命中的强制术语（含别名）：{中文称呼: {"en": 标准译名, "variants": [...]}}。

    同一实体的多种中文称呼（如「边缘酒吧」「荒原边缘酒吧」）全部映射到同一译名，
    场景名/专有名词强制复用首次确定的译名，杜绝同一地点多种译名。
    variants 声明译名合法变体（复数、时态等），词边界匹配不再误伤 werewolves。
    """
    forced: dict[str, dict] = {}
    for k in db.scalars(
        select(KnowledgeItem).where(
            KnowledgeItem.project_id == project_id,
            KnowledgeItem.item_type == "term",
            KnowledgeItem.status.in_(["approved", "auto_approved"]),
        )
    ):
        data = json.loads(k.data_json or "{}")
        translation = (data.get("translation") or "").strip()
        if not translation or translation == k.canonical_key:
            continue
        variants = [
            v.strip() for v in (data.get("variants") or [])
            if isinstance(v, str) and v.strip()
        ]
        keys = [k.canonical_key]
        for a in db.scalars(
            select(KnowledgeAlias.alias).where(KnowledgeAlias.knowledge_id == k.id)
        ):
            if a and a != k.canonical_key and a not in keys:
                keys.append(a)
        for key in keys:
            if key in text:
                forced[key] = {"en": translation, "variants": variants}
    return forced


def _window_forced_terms(db: Session, project_id: str, window: list[SourceSegment]) -> dict[str, str]:
    """窗口内命中的强制术语：{术语中文: 必须出现的英文}。"""
    win_text = "".join(s.source_text for s in window)
    return _forced_terms_for_text(db, project_id, win_text)


async def process_window_qa_item(db_factory, task_id: str, item_id: str, model: str) -> dict:
    """独立 QA 任务节点：定点查询（确定性 QA 定位问题段）+ 定点修改（润色模型只重译问题段）。

    与窗口翻译解耦：QA 打回绝不重启初翻/润色任务，本节点失败/超时也只重跑自身。
    最多 3 轮修复；仍未修复的段标 needs_review 转人工，不再自动重译。
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
        # 本窗口最新 polished（QA 只检查/修改润色层，初翻不参与）
        rows = list(
            db.scalars(
                select(Translation).where(
                    Translation.segment_id.in_([s.id for s in window]),
                    Translation.stage == "polished",
                ).order_by(Translation.version.desc())
            )
        )
        polished: dict[str, dict] = {}
        for t in rows:
            polished.setdefault(t.segment_id, t)
        forced = _window_forced_terms(db, project_id, window)
        draft_rows = list(
            db.scalars(
                select(Translation).where(
                    Translation.segment_id.in_([s.id for s in window]),
                    Translation.stage == "draft",
                ).order_by(Translation.version.desc())
            )
        )
        draft_latest: dict[str, Translation] = {}
        for t in draft_rows:
            draft_latest.setdefault(t.segment_id, t)

    seg_by_id = {s.id: s for s in window}
    provider = get_provider()
    issues_total = 0
    repaired = 0
    leftover: list[str] = []
    for _ in range(3):
        with db_factory() as db:
            forced = _window_forced_terms(db, project_id, window)
            seg_fix: dict[str, list[dict]] = {}
            for seg in window:
                tr = polished.get(seg.id)
                if tr is None:
                    continue
                det = qa_service.deterministic_qa(db, seg, tr, forced)
                need_fix = [i for i in det if qa_service.should_repair(i)]
                if need_fix:
                    seg_fix[seg.id] = need_fix
                issues_total += len(det)
        if not seg_fix:
            break
        # 定点修改：润色模型只重译问题段（窗口其余段保持不变，初翻不参与）
        target = sorted(seg_fix)
        context = ContextResult(
            prompt_text=(
                "SOURCE SEGMENTS (translate only these, one translation per segment_id):\n"
                + "\n".join(f"[{s.id}] {s.source_text}" for s in window if s.id in target)
            )
        )
        draft = [
            {"segment_id": s.id, "target_text": draft_latest[s.id].target_text}
            for s in window if s.id in target and s.id in draft_latest
        ]
        try:
            fixed_map = await repair_segments(
                provider=provider, model=model, window_id=f"QA-{item.window_id}",
                context=context, draft=draft,
                issues=[i for lst in seg_fix.values() for i in lst],
                seg_ids=target,
            )
        except Exception:  # noqa: BLE001 定点修复异常：保留原译文，不阻塞
            fixed_map = {}
        if not fixed_map:
            leftover = [sid for sid in target]
            break
        fixed_ok: dict[str, str] = {}
        for sid in target:
            text = (fixed_map.get(sid) or "").strip()
            seg = seg_by_id.get(sid)
            if not text or seg is None:
                continue
            if validate_window(
                [{"segment_id": sid, "source_text": seg.source_text}],
                [{"segment_id": sid, "target_text": text}],
            ):
                continue  # 校验不通过：不写库
            fixed_ok[sid] = text
        if not fixed_ok:
            leftover = [sid for sid in target]
            break
        with db_factory() as db:
            for sid, text in fixed_ok.items():
                insert_translation_row(
                    db, segment_id=sid, stage="polished", target_text=text,
                    review_status="unreviewed", freshness_status="current",
                    notes_json="[]", ambiguity_json="[]",
                    model_name=model, provider_id="default",
                    prompt_version=PROMPT_VERSION, context_snapshot_json="{}",
                    source_hash=sha256(seg_by_id[sid].source_text),
                )
            db.commit()
        repaired += len(fixed_ok)
        # 修复段替换进内存，供下一轮重检
        for sid, text in fixed_ok.items():
            t = polished.get(sid)
            if t is not None:
                t.target_text = text
        # 重检修复段：仍有应修问题的进入下一轮
        still_bad: list[str] = []
        with db_factory() as db:
            forced = _window_forced_terms(db, project_id, window)
            for sid in fixed_ok:
                seg = seg_by_id[sid]
                det = qa_service.deterministic_qa(db, seg, polished[sid], forced)
                if any(qa_service.should_repair(i) for i in det):
                    still_bad.append(sid)
        if not still_bad:
            break
        leftover = still_bad
    # 超限未修复段：标 needs_review 转人工（不触发任何任务重跑）
    if leftover:
        with db_factory() as db:
            for sid in leftover:
                t = db.scalar(
                    select(Translation).where(
                        Translation.segment_id == sid, Translation.stage == "polished"
                    ).order_by(Translation.version.desc()).limit(1)
                )
                if t is not None:
                    t.review_status = "needs_review"
            db.commit()

    with db_factory() as db:
        item = db.get(TaskItem, item_id)
        item.status = "succeeded"
        item.finished_at = func.now()
        db.commit()
    return {"done": 1, "window_id": item.window_id, "qa_issues": issues_total,
            "repaired": repaired}


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
        kw = dict(
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
        provider=get_provider(), model=model, window_id="DRAFT", context=context,
        source_chars=sum(len(s["source_text"]) for s in seg_dicts),
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
    errors = validate_window(
        [{"segment_id": s.id, "source_text": s.source_text} for s in window], draft
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
