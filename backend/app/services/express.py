"""Express 一键直出编排：知识 → 场景卡 → 试译/风格 → 全本翻译 → QA → 批量接受。"""

import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.protocol import LLMError
from app.llm.registry import get_provider
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.qa import WindowQuality
from app.models.translation import Translation
from app.services import (
    continuity_service,
    knowledge_service,
    profile_service,
    qa_service,
    storyline_service,
    style_guide_service,
)
from app.services.importer import sha256
from app.services.translation import (
    PROMPT_VERSION,
    locate_problem_segments,
    repair_segments,
    validate_window,
)
from app.services.title_service import preserved_title_for_segment
WINDOW_CONCURRENCY = max(1, int(os.environ.get("MT_WINDOW_CONCURRENCY", "3")))


def _window_concurrency_for(model: str) -> int:
    """Flash 的上下文缓存和吞吐更稳定，默认并发 5；其他模型保持 3。

    显式设置 MT_WINDOW_CONCURRENCY 时始终以用户配置为准。
    """
    configured = os.environ.get("MT_WINDOW_CONCURRENCY")
    if configured:
        return max(1, int(configured))
    return 5 if "flash" in (model or "").lower() else WINDOW_CONCURRENCY
# 结构修复只做一次、且必须有限时。历史上“整窗拆半重润色 + 逐段修复”会在
# 99/99 后继续串行发请求，用户看到长时间无进度；这里把最坏成本封死。
STRUCTURE_REPAIR_TIMEOUT = max(15, int(os.environ.get("MT_STRUCTURE_REPAIR_TIMEOUT", "60")))
STRUCTURE_REPAIR_MAX_SEGMENTS = max(1, int(os.environ.get("MT_STRUCTURE_REPAIR_MAX_SEGMENTS", "8")))
STRUCTURE_REPAIR_MAX_TOTAL = max(1, int(os.environ.get("MT_STRUCTURE_REPAIR_MAX_TOTAL", "20")))
QA_REPAIR_TIMEOUT = max(15, int(os.environ.get("MT_QA_REPAIR_TIMEOUT", "90")))

from app.services.translation_batch import (
    _canonicalize_polished_terms,
    _forced_terms_for_text,
    _load_window_context,
    _persist_window_translations,
    _window_material,
    _speaker_map_for_project,
    insert_translation_row,
    persist_naturalness_edits,
    translate_window_draft_direct,
    translate_window_polish_direct,
)
from app.services.context_builder import build_qa_reference
from app.services.term_match import normalize, select_forced_specs_for_text, target_hits_term
from app.services.window_planner import build_windows


def _sync_progress(
    db_factory, task_id: str, steps: dict, current_step: str, extra: dict | None = None
) -> None:
    """实时写任务进度（前端任务中心 2 秒轮询展示）。"""
    from app.models.task import Task

    with db_factory() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        payload = {"steps": steps, "current_step": current_step}
        if extra:
            payload.update(extra)
        task.progress_json = json.dumps(payload, ensure_ascii=False)
        db.commit()


async def _repair_express_qa(
    db_factory, project_id: str, window, win_issues: list[dict],
    provider, model: str, forced: dict,
) -> tuple[int, list[str]]:
    """Express QA 打回定点修复：AI 只修问题段（medium/high/critical），最多 3 轮。

    每轮修复段写库新版本；重检（确定性 QA）仍有问题的段转 needs_review，不再自动重译。
    返回 (修复段数, 未修复段 id 列表)。
    """
    import asyncio as _aio

    from app.services.translation_batch import insert_translation_row

    target_ids = sorted({
        i.get("segment_id") for i in win_issues
        if qa_service.should_repair(i) and i.get("segment_id")
    })
    if not target_ids:
        return 0, []
    seg_by_id = {s.id: s for s in window}
    with db_factory() as db:
        speaker_map = _speaker_map_for_project(db, project_id)
        draft_rows = list(
            db.scalars(
                select(Translation).where(
                    Translation.segment_id.in_([s.id for s in window]),
                    Translation.stage == "draft",
                ).order_by(Translation.version.desc())
            )
        )
    draft_latest: dict[str, Translation] = {}
    hashes = {s.id: s.source_hash for s in window}
    for t in draft_rows:
        if t.source_hash == hashes.get(t.segment_id) and t.freshness_status == "current":
            draft_latest.setdefault(t.segment_id, t)
    draft = [
        {"segment_id": s.id, "target_text": draft_latest[s.id].target_text}
        for s in window
        if s.id in target_ids and s.id in draft_latest
    ]
    target_window = [s for s in window if s.id in target_ids]
    _sd, ctx, _sg, _card = await _window_material(
        db_factory, project_id, target_window, None
    )
    fixed_total = 0
    remaining = list(target_ids)
    for _ in range(3):
        if not remaining:
            break
        issue_payload = [
            i for i in win_issues if i.get("segment_id") in remaining
        ]
        try:
            # high/critical 可能涉及代词/否定等语义改写，关闭思考时模型容易
            # 原样返回；这类修复启用思考，medium 仍走低成本路径。
            has_high = any(
                i.get("severity") in ("high", "critical")
                for i in issue_payload
            )
            fixed_map = await _aio.wait_for(
                repair_segments(
                    provider=provider, model=model, window_id="QA-FIX-EXPRESS",
                    context=ctx, draft=draft,
                    issues=issue_payload,
                    seg_ids=remaining,
                    thinking="enabled" if has_high else "disabled",
                ),
                timeout=QA_REPAIR_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 修复异常：保留原译文转人工
            fixed_map = {}
        if not fixed_map:
            break
        fixed_ok: dict[str, str] = {}
        for sid in list(remaining):
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
                continue  # 校验不通过：该段不写库
            fixed_ok[sid] = text
        if not fixed_ok:
            break
        with db_factory() as db:
            for sid, text in fixed_ok.items():
                insert_translation_row(
                    db, segment_id=sid, stage="polished", target_text=text,
                    review_status="unreviewed", freshness_status="current",
                    notes_json="[]", ambiguity_json="[]",
                    model_name=model, provider_id="default",
                    prompt_version=PROMPT_VERSION,
                    context_snapshot_json=json.dumps(ctx.snapshot, ensure_ascii=False),
                    source_hash=sha256(seg_by_id[sid].source_text),
                )
            db.commit()
        fixed_total += len(fixed_ok)
        # 重检修复段：确定性 QA 仍有 medium/high/critical 才算未修复
        with db_factory() as db:
            still_bad: list[str] = []
            for sid in fixed_ok:
                seg = seg_by_id[sid]
                class _FakeTr:
                    target_text = fixed_ok[sid]
                det = qa_service.deterministic_qa(db, seg, _FakeTr(), forced)
                if any(qa_service.should_repair(i) for i in det):
                    still_bad.append(sid)
        remaining = [sid for sid in remaining if sid in still_bad]
    if remaining:
        with db_factory() as db:
            for sid in remaining:
                t = db.scalar(
                    select(Translation).where(
                        Translation.segment_id == sid, Translation.stage == "polished"
                    ).order_by(Translation.version.desc()).limit(1)
                )
                if t is not None:
                    t.review_status = "needs_review"
            db.commit()
    return fixed_total, remaining


async def run_express(db_factory, project_id: str, source_document_id: str, model: str, task_id: str = "express") -> dict:
    import asyncio as _aio

    provider = get_provider()
    window_concurrency = _window_concurrency_for(model)

    def _paused() -> bool:
        with db_factory() as pdb:
            t = pdb.get(Task, task_id)
            return t is not None and t.status == "paused"

    # 断点续跑：读取上次进度，跳过已完成步骤（任务失败后重试不从头再来）
    from app.models.task import Task

    report: dict = {"steps": {}}
    with db_factory() as db:
        task = db.get(Task, task_id)
        prev_raw = task.progress_json if task else "{}"
    try:
        prev = json.loads(prev_raw or "{}")
    except json.JSONDecodeError:
        prev = {}
    if isinstance(prev, dict) and isinstance(prev.get("steps"), dict):
        report["steps"] = dict(prev["steps"])

    # 1. 知识提取（Express：explicit 的 character/term 自动批准）
    if "knowledge" not in report["steps"]:
        _sync_progress(db_factory, task_id, report["steps"], "knowledge")
        knowledge_result = await _aio.wait_for(
            knowledge_service.extract_knowledge_for_document(
                db_factory, project_id, source_document_id, provider, model, mode="express"
            ),
            timeout=2400,  # 整步硬超时：防止知识提取挂起卡住后续流程
        )
        if int(knowledge_result.get("failed_batches") or 0) > 0:
            # 不能带着残缺知识继续全本翻译：否则术语/人物/地点会在后文漂移。
            last_error = (knowledge_result.get("errors") or ["未知错误"])[0]
            report["steps"].pop("knowledge", None)
            _sync_progress(db_factory, task_id, report["steps"], "knowledge")
            raise RuntimeError(
                f"知识提取有 {knowledge_result['failed_batches']} 个批次失败，"
                f"已停止翻译以避免全文不一致。请重试；原因：{last_error}"
            )
        report["steps"]["knowledge"] = knowledge_result
    # 2. 准备阶段协程：profiles/storylines/dialogue/trial 互不依赖，
    #    且 Draft 只锁事实、不需要 Style Guide —— 四者与 Draft 并行，缩短关键路径
    async def _prep_profiles():
        if "profiles" not in report["steps"]:
            report["steps"]["profiles"] = await profile_service.extract_profiles(
                db_factory, project_id, source_document_id, provider, model
            )
        _sync_progress(db_factory, task_id, report["steps"], "profiles")

    async def _prep_dialogue():
        if "extracted" not in (report["steps"].get("dialogue") or {}):
            from app.services import dialogue_service

            def _dl_cb(p: dict) -> None:
                report["steps"]["dialogue"] = {
                    "done_batches": p["done_batches"],
                    "total_batches": p["total_batches"],
                    "lines": p["lines"],
                    "discarded": p["discarded"],
                    "failed_batches": p["failed_batches"],
                }
                _sync_progress(db_factory, task_id, report["steps"], "dialogue")

            report["steps"]["dialogue"] = await dialogue_service.extract_dialogue_lines(
                db_factory, project_id, source_document_id, provider, model, progress_cb=_dl_cb
            )
        _sync_progress(db_factory, task_id, report["steps"], "dialogue")

    async def _prep_trial():
        # 试译与风格基线（Polish 依赖 Style Guide）
        if "trial" not in report["steps"]:
            report["steps"]["trial"] = await style_guide_service.run_trial(
                db_factory, project_id, source_document_id, provider, model
            )
        _sync_progress(db_factory, task_id, report["steps"], "trial")

    _sync_progress(db_factory, task_id, report["steps"], "translation")
    # 4. 全本窗口翻译：Draft/Polish 流水线并行。
    #    Polish 等自身及前一窗口 Draft、术语门控、全局角色档案/风格基线，
    #    以及“本窗口所属章节”的剧情卡就绪后启动；不等待无关章节或台词本。
    #    有待定术语时退回全量 Draft 后锁名的安全路径。
    with db_factory() as db:
        segs = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.source_document_id == source_document_id)
                .order_by(SourceSegment.segment_no)
            )
        )
        speaker_map = _speaker_map_for_project(db, project_id)
    windows = build_windows(segs)
    total = len(windows)

    # 续跑以数据库实际译文为准，不用 progress.done 推算窗口起点：
    # 并发下后段可能先完成，done 数字无法代表“哪些窗口已完成”。
    with db_factory() as db:
        latest_rows = list(
            db.scalars(
                select(Translation)
                .where(Translation.segment_id.in_([s.id for s in segs]))
                .order_by(Translation.version.desc())
            )
        )
    latest_by_key: dict[tuple[str, str], Translation] = {}
    for t in latest_rows:
        latest_by_key.setdefault((t.segment_id, t.stage), t)

    # 老项目在升级前可能已有剧情卡但没有结构化状态：任务开始时确定性回填。
    try:
        with db_factory() as db:
            report["steps"]["continuity"] = continuity_service.sync_project_continuity(
                db, project_id
            )
    except Exception as exc:  # noqa: BLE001 派生状态失败不阻断翻译，沿用剧情卡提示
        report["steps"]["continuity"] = {"error": str(exc)[:300]}

    def _window_stage_complete(win, stage: str) -> bool:
        for seg in win:
            t = latest_by_key.get((seg.id, stage))
            if (
                t is None
                or not (t.target_text or "").strip()
                or t.source_hash != seg.source_hash
                or t.freshness_status != "current"
            ):
                return False
        return True

    polished_complete = {
        i for i, win in enumerate(windows) if _window_stage_complete(win, "polished")
    }
    draft_complete = {
        i for i, win in enumerate(windows) if _window_stage_complete(win, "draft")
    }
    done = len(polished_complete)
    draft_done_count = len(draft_complete)
    prev_trans = report["steps"].get("translation") or {}
    # Draft carry map：续跑时从进度恢复；新完成的窗口会立即写入。
    draft_carry_map: dict[str, dict] = dict(prev_trans.get("draft_carry") or {})
    # 只调度尚未润色的窗口；已完成窗口保持原译文，不重新调用模型。
    todo_indices = [i for i in range(total) if i not in polished_complete]
    todo = [windows[i] for i in todo_indices]
    if todo_indices:
        # 本次会生成新的 polished 版本，旧的台词对齐/QA/术语一致性/定稿结果失效，
        # 必须基于新译文重新执行；已完成窗口的翻译本身仍不重跑。
        for dependent_step in ("dialogue_align", "qa", "term_consistency", "approve"):
            report["steps"].pop(dependent_step, None)
    report["steps"]["translation"] = {
        "done": done,
        "total": total,
        "phase": "draft",
        "draft_done": draft_done_count,
        "draft_carry": draft_carry_map,
    }
    _sync_progress(db_factory, task_id, report["steps"], "translation")

    # 阶段 1：未完成窗口的 Draft 并发（Draft 不依赖 carry-over）
    _sem = _aio.Semaphore(window_concurrency)

    def _draft_from_db(db, win) -> list[dict]:
        out = []
        for seg in win:
            t = db.scalar(
                select(Translation)
                .where(Translation.segment_id == seg.id, Translation.stage == "draft")
                .order_by(Translation.version.desc())
                .limit(1)
            )
            out.append({
                "segment_id": seg.id,
                "target_text": t.target_text if t else "",
                "notes": [],
                "ambiguity": [],
            })
        return out

    async def _draft_one(win_idx, win):
        nonlocal draft_done_count, draft_carry_map
        if _paused():
            return None
        carry_key = f"WIN_{win_idx:04d}"
        # 已初翻窗口直接复用 DB Draft；carry 缺失也不重跑 Draft（旧版本/中断兼容）。
        if win_idx in draft_complete:
            carry = draft_carry_map.get(carry_key)
            with db_factory() as db:
                reused_draft = _draft_from_db(db, win)
            # 复用 Draft 时仍需构建上下文快照，供润色与后续落库/追溯使用。
            _sd, context, _sg, _card = await _window_material(
                db_factory, project_id, win, carry
            )
            return reused_draft, carry, context
        async with _sem:
            if _paused():
                return None
            try:
                result = await _aio.wait_for(
                    translate_window_draft_direct(
                        db_factory, project_id, win, model,
                        task_id=task_id, window_key=f"WIN_{win_idx:04d}",
                    ),
                    timeout=240,
                )
            except Exception:  # noqa: BLE001 超时/异常：该窗口标记失败跳过，不卡住整本
                return None
        if result is not None and result[1] is not None:
            draft_carry_map[carry_key] = result[1]
        # 实时进度：每完成一窗写一次，carry 也随窗口立即持久化，
        # 进程/任务中途失败后无需为了重建 carry 再跑一遍 Draft。
        draft_done_count += 1
        report["steps"]["translation"]["draft_done"] = draft_done_count
        report["steps"]["translation"]["draft_carry"] = draft_carry_map
        _sync_progress(db_factory, task_id, report["steps"], "translation")
        return result

    async def _lock_terms_once():
        """Draft 全量完成后为无译名术语统一定名，Polish 强制使用（跨窗口一致性）。"""
        if report["steps"].get("terms_locked"):
            return
        from app.services.term_lock import lock_untranslated_terms

        result = await lock_untranslated_terms(
            db_factory, project_id, source_document_id, provider, model
        )
        report["steps"]["terms_locked"] = {"locked": result.get("locked", 0)}
        _sync_progress(db_factory, task_id, report["steps"], "translation")

    # 并行流水线：Draft 立即启动；准备资料和术语门控异步推进。
    # 无待定术语时 Polish 可在首个窗口 Draft 完成后立即开始；
    # 有待定术语时自动等待全量 Draft + 术语锁定，保持原有质量路径。
    from app.services.term_lock import has_pending_untranslated_terms

    with db_factory() as db:
        pending_terms = has_pending_untranslated_terms(db, project_id)

    draft_tasks = {
        wi: _aio.create_task(_draft_one(wi, windows[wi])) for wi in todo_indices
    }

    profiles_ready = _aio.Event()
    profiles_errors: list[Exception] = []
    style_ready = _aio.Event()
    style_errors: list[Exception] = []
    storyline_errors: list[Exception] = []
    dialogue_errors: list[Exception] = []
    chapter_ids = {w[0].chapter_id for w in windows if w and w[0].chapter_id}
    storyline_ready = {cid: _aio.Event() for cid in chapter_ids}

    async def _run_profiles_gate():
        try:
            await _aio.wait_for(_prep_profiles(), timeout=600)
        except Exception as exc:  # noqa: BLE001
            profiles_errors.append(exc)
        finally:
            profiles_ready.set()

    async def _run_storylines_gate():
        try:
            if "storylines" not in report["steps"]:
                def _chapter_done(chapter_id: str) -> None:
                    event = storyline_ready.get(chapter_id)
                    if event is not None:
                        event.set()

                report["steps"]["storylines"] = await _aio.wait_for(
                    storyline_service.generate_for_document(
                        db_factory, project_id, source_document_id, provider, model,
                        on_chapter_done=_chapter_done,
                    ),
                    timeout=2400,
                )
            _sync_progress(db_factory, task_id, report["steps"], "storylines")
        except Exception as exc:  # noqa: BLE001 全局剧情卡失败才阻塞，单章失败由服务内部标记
            storyline_errors.append(exc)
        finally:
            # 异常/续跑场景释放所有章节等待，避免 Polish 死等。
            for event in storyline_ready.values():
                event.set()

    async def _run_style_gate():
        try:
            await _aio.wait_for(_prep_trial(), timeout=2400)
        except Exception as exc:  # noqa: BLE001
            style_errors.append(exc)
        finally:
            style_ready.set()

    async def _run_dialogue_background():
        try:
            await _aio.wait_for(_prep_dialogue(), timeout=600)
        except Exception as exc:  # noqa: BLE001 台词本不阻塞润色，但会在 QA 前报错
            dialogue_errors.append(exc)

    terms_ready = _aio.Event()
    term_errors: list[Exception] = []

    async def _run_terms_gate():
        try:
            if pending_terms:
                if draft_tasks:
                    await _aio.gather(*draft_tasks.values())
                await _lock_terms_once()
            else:
                report["steps"]["terms_locked"] = {"locked": 0, "pipeline": True}
                _sync_progress(db_factory, task_id, report["steps"], "translation")
        except Exception as exc:  # noqa: BLE001 术语门控失败不得让 Polish 使用未锁定术语
            term_errors.append(exc)
        finally:
            terms_ready.set()

    profiles_task = _aio.create_task(_run_profiles_gate())
    storylines_task = _aio.create_task(_run_storylines_gate())
    style_task = _aio.create_task(_run_style_gate())
    dialogue_task = _aio.create_task(_run_dialogue_background())
    terms_task = _aio.create_task(_run_terms_gate())

    # Polish 流水线实时活动：Draft 未结束时 phase 仍为 draft，
    # 但 model_done/active_polish 会同步展示已开始的润色窗口。
    polish_base_done = done
    polish_active = 0
    polish_processed = 0
    polish_model_total = len(todo_indices)

    def _sync_polish_live() -> None:
        tr = report["steps"]["translation"]
        tr["active_polish"] = polish_active
        tr["model_done"] = polish_processed
        tr["model_total"] = polish_model_total
        tr["base_done"] = polish_base_done
        _sync_progress(db_factory, task_id, report["steps"], "translation")

    report["steps"]["translation"].update({
        "active_polish": 0,
        "model_done": 0,
        "model_total": polish_model_total,
        "base_done": polish_base_done,
    })
    _sync_progress(db_factory, task_id, report["steps"], "translation")
    _sem2 = _aio.Semaphore(window_concurrency)

    def _prev_carry(win_idx: int) -> dict | None:
        if win_idx <= 0:
            return None
        return draft_carry_map.get(f"WIN_{win_idx - 1:04d}")

    def _window_polished_complete(db, win) -> bool:
        """窗口所有段都有非空 polished 译文 → 可跳过（重试不重做已完成窗口）。"""
        for seg in win:
            t = db.scalar(
                select(Translation)
                .where(Translation.segment_id == seg.id, Translation.stage == "polished")
                .order_by(Translation.version.desc())
                .limit(1)
            )
            if (
                t is None
                or not (t.target_text or "").strip()
                or t.source_hash != seg.source_hash
                or t.freshness_status != "current"
            ):
                return False
        return True

    def _polished_from_db(db, win) -> list[dict]:
        out = []
        for seg in win:
            t = db.scalar(
                select(Translation)
                .where(Translation.segment_id == seg.id, Translation.stage == "polished")
                .order_by(Translation.version.desc())
                .limit(1)
            )
            out.append({
                "segment_id": seg.id,
                "target_text": t.target_text if t else "",
                "notes": [],
                "ambiguity": [],
            })
        return out

    polish_errors: dict[int, str] = {}
    permanent_failures: set[int] = set()

    async def _polish_one(win_idx, window, retries: int = 2):
        nonlocal polish_active, polish_processed
        if _paused():
            return None
        draft_res = await draft_tasks[win_idx]
        # 窗口 i 的 carry 来自 i-1 的 Draft：必须等前一窗口 Draft 完成；
        # 若前一窗口已润色完成（不在本轮 todo 中），沿用已有 carry/DB 快照。
        if win_idx > 0 and (win_idx - 1) in draft_tasks:
            await draft_tasks[win_idx - 1]
        await profiles_ready.wait()
        await style_ready.wait()
        chapter_id = window[0].chapter_id if window else None
        chapter_event = storyline_ready.get(chapter_id) if chapter_id else None
        if chapter_event is not None:
            await chapter_event.wait()
        await terms_ready.wait()
        if profiles_errors:
            polish_errors[win_idx] = f"角色档案失败：{profiles_errors[0]}"
            return "failed"
        if style_errors:
            polish_errors[win_idx] = f"风格基线失败：{style_errors[0]}"
            return "failed"
        if storyline_errors:
            polish_errors[win_idx] = f"剧情卡全局失败：{storyline_errors[0]}"
            return "failed"
        if term_errors:
            polish_errors[win_idx] = f"术语锁定失败：{term_errors[0]}"
            return "failed"
        if _paused():
            return None
        draft = draft_res[0] if draft_res else None
        if draft is None:
            polish_errors[win_idx] = "draft 缺失"
            return "failed"
        # 断点续跑：窗口 polished 已完成 → 直接复用，不重做
        with db_factory() as db:
            if _window_polished_complete(db, window):
                polish_processed = min(polish_model_total, polish_processed + 1)
                _sync_polish_live()
                return _polished_from_db(db, window), None
        last_error = ""
        for attempt in range(retries + 1):
            if _paused():
                return None
            async with _sem2:
                polish_active += 1
                _sync_polish_live()
                try:
                    return await _aio.wait_for(
                        translate_window_polish_direct(
                            db_factory, project_id, window, draft, _prev_carry(win_idx), model,
                            task_id=task_id, window_key=f"WIN_{win_idx:04d}",
                        ),
                        timeout=240,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                    polish_errors[win_idx] = last_error
                    # 输出截断/认证/请求错误不是瞬时故障：重复请求只会增加费用。
                    if isinstance(exc, LLMError) and not exc.retryable:
                        permanent_failures.add(win_idx)
                        break
                finally:
                    polish_active = max(0, polish_active - 1)
                    polish_processed = min(polish_model_total, polish_processed + 1)
                    _sync_polish_live()
            if attempt < retries:
                await _aio.sleep(3 * (attempt + 1))
        return "failed"

    def _seg_dicts(window) -> list[dict]:
        return [
            {
                "segment_id": s.id,
                "segment_type": s.segment_type,
                "speaker_name": s.speaker_name,
                "source_text": s.source_text,
                "line_count_optional": bool(preserved_title_for_segment(s)),
            }
            for s in window
        ]

    def _line_counts(window) -> dict[str, int]:
        return {
            s.id: len([x for x in s.source_text.strip().splitlines() if x.strip()])
            for s in window
        }

    def _apply_fixed_map(polished: list[dict], fixed_map: dict[str, str]) -> list[dict]:
        """按 segment_id 替换问题段；重复段只保留第一份，缺失段补回。"""
        if not fixed_map:
            return polished
        out: list[dict] = []
        seen_fixed: set[str] = set()
        for entry in polished:
            sid = entry.get("segment_id")
            if sid not in fixed_map:
                out.append(entry)
                continue
            if sid in seen_fixed:
                continue
            seen_fixed.add(sid)
            out.append({**entry, "target_text": fixed_map[sid]})
        for sid, text in fixed_map.items():
            if sid not in seen_fixed:
                out.append({
                    "segment_id": sid, "target_text": text,
                    "notes": [], "ambiguity": [],
                })
        return out

    def _draft_structural_fallback(
        window, polished: list[dict], draft: list[dict]
    ) -> tuple[list[dict], list[str], list[str]]:
        """结构异常段的零调用兜底：Draft 已锁事实且结构合法时直接采用。

        结构校验失败通常只是 Polish 合并了行、丢了角色标签或错误挪段。
        重新让模型润色整窗既慢又可能继续犯错；Draft 已逐段保留原文结构，
        因此先用它兜底，只有 Draft 也无效时才允许一次限时修复。
        """
        seg_dicts = _seg_dicts(window)
        errors = validate_window(seg_dicts, polished, speaker_map=speaker_map)
        if not errors:
            return polished, [], []
        bad_ids = locate_problem_segments(seg_dicts, polished, speaker_map=speaker_map)
        if not bad_ids:
            return polished, errors, []
        seg_by_id = {s.id: s for s in window}
        draft_by_id = {e.get("segment_id"): e for e in draft if e.get("target_text")}
        replacements: dict[str, str] = {}
        for sid in bad_ids:
            seg = seg_by_id.get(sid)
            candidate = draft_by_id.get(sid)
            text = (candidate or {}).get("target_text") or ""
            if seg is None or not str(text).strip():
                continue
            one_errors = validate_window(
                _seg_dicts([seg]),
                [{"segment_id": sid, "target_text": text}],
                speaker_map=speaker_map,
            )
            if not one_errors:
                replacements[sid] = str(text)
        if not replacements:
            return polished, errors, []
        out = _apply_fixed_map(polished, replacements)
        for entry in out:
            sid = entry.get("segment_id")
            if sid in replacements:
                notes = list(entry.get("notes") or [])
                if "structure-fallback:draft" not in notes:
                    notes.append("structure-fallback:draft")
                entry["notes"] = notes
        return (
            out,
            validate_window(seg_dicts, out, speaker_map=speaker_map),
            sorted(replacements),
        )

    polish_jobs = list(zip(todo_indices, todo))
    polish_tasks = [
        _aio.create_task(_polish_one(wi, w)) for wi, w in polish_jobs
    ]

    # 等 Draft 与准备/术语门控完成后，再收集 Polish 结果；Polish 任务已在后台等待依赖。
    if draft_tasks:
        ordered_drafts = await _aio.gather(*(draft_tasks[wi] for wi in todo_indices))
        draft_results = list(ordered_drafts)
        # Draft 全部结束，切换任务中心阶段显示；已提前完成的 Polish 数据保留。
        report["steps"]["translation"]["phase"] = "polish"
        report["steps"]["translation"]["draft_done"] = draft_done_count
        report["steps"]["translation"]["draft_carry"] = draft_carry_map
        _sync_progress(db_factory, task_id, report["steps"], "translation")
    else:
        draft_results = []
    await profiles_task
    await storylines_task
    await style_task
    await dialogue_task
    await terms_task
    polished_results = await _aio.gather(*polish_tasks)
    if profiles_errors:
        raise profiles_errors[0]
    if style_errors:
        raise style_errors[0]
    if storyline_errors:
        raise storyline_errors[0]
    if dialogue_errors:
        raise dialogue_errors[0]
    if term_errors:
        raise term_errors[0]

    # 并发结束后只补跑一次瞬时失败的窗口；永久错误（截断/认证）绝不重复烧钱。
    failed_positions = [
        i for i, pr in enumerate(polished_results)
        if pr == "failed" and polish_jobs[i][0] not in permanent_failures
    ]
    if failed_positions and not _paused():
        await _aio.sleep(5)
        for pos in failed_positions:
            wi, w = polish_jobs[pos]
            polished_results[pos] = await _polish_one(wi, w, retries=1)

    # 顺序持久化与进度上报（单协程写，无竞争）。
    # 结构校验/兜底也在这里逐窗推进并写进度：用户不会在出现 99/99 后
    # 盯着一个没有变化的数字等待一串不可见的修复请求。
    failed_windows = 0
    structural_fallback_ids: list[str] = []
    structure_repair_attempts = 0
    tr_state = report["steps"]["translation"]
    tr_state.update({
        "phase": "finalize",
        "finalize_done": 0,
        "structure_fallbacks": 0,
        "structure_repairs": 0,
    })
    _sync_progress(db_factory, task_id, report["steps"], "translation")

    for pos, (wi, window, dr, pr) in enumerate(
        zip(todo_indices, todo, draft_results, polished_results)
    ):
        tr_state["finalize_done"] = pos
        tr_state["repairing_window"] = None
        _sync_progress(db_factory, task_id, report["steps"], "translation")
        if pr is None:
            if _paused():
                break  # 暂停：保留进度，resume 续跑
            failed_windows += 1
            report["steps"]["translation"]["failed_windows"] = failed_windows
            _sync_progress(db_factory, task_id, report["steps"], "translation")
            continue
        if pr == "failed":
            failed_windows += 1
            report["steps"]["translation"]["failed_windows"] = failed_windows
            _sync_progress(db_factory, task_id, report["steps"], "translation")
            continue  # 不落库失败窗口；可重试或按章节重译
        if dr is None:
            continue  # Draft 失败窗口无材料，跳过
        draft, context = dr[0], dr[2]
        polished = pr[0]
        if pr[1] is None:
            # 已完成窗口复用：仅推进进度，不重复落库
            done += 1
            tr_state.update({
                "done": done,
                "total": total,
                "phase": "finalize",
                "draft_done": draft_done_count,
                "draft_carry": draft_carry_map,
                "finalize_done": pos + 1,
            })
            _sync_polish_live()
            continue
        # 段完整性校验：缺行/错位是历史漏译与标签错位的根因，拒绝静默落库
        seg_dicts = _seg_dicts(window)
        line_counts = _line_counts(window)
        errors = validate_window(seg_dicts, polished, speaker_map=speaker_map)
        if errors:
            tr_state["phase"] = "repair"
            tr_state["repairing_window"] = wi + 1
            _sync_progress(db_factory, task_id, report["steps"], "translation")
            # 第一层：零模型调用。Draft 已锁事实，结构合法时直接采用。
            polished, errors, fallback_ids = _draft_structural_fallback(
                window, polished, draft
            )
            if fallback_ids:
                structural_fallback_ids.extend(fallback_ids)
                tr_state["structure_fallbacks"] = len(structural_fallback_ids)
                _sync_progress(db_factory, task_id, report["steps"], "translation")
            if errors:
                # 第二层：只对 Draft 也无效的段做一次限时定点修复，且关闭思考。
                bad_ids = sorted(locate_problem_segments(
                    seg_dicts, polished, speaker_map=speaker_map
                ))
                repair_ids = bad_ids[:STRUCTURE_REPAIR_MAX_SEGMENTS]
                if (
                    repair_ids
                    and structure_repair_attempts < min(total, STRUCTURE_REPAIR_MAX_TOTAL)
                ):
                    structure_repair_attempts += 1
                    tr_state["structure_repairs"] = structure_repair_attempts
                    try:
                        fix_issues = [
                            {"issue_type": "line_shift", "explanation": e,
                             "suggested_fix": "补齐缺失行，逐行对应原文"}
                            for e in errors
                        ]
                        fixed_map = await _aio.wait_for(
                            repair_segments(
                                provider=provider, model=model,
                                window_id=f"FIX-{window[0].segment_no}",
                                context=context, draft=draft, issues=fix_issues,
                                seg_ids=repair_ids, line_counts=line_counts,
                                thinking="disabled",
                            ),
                            timeout=STRUCTURE_REPAIR_TIMEOUT,
                        )
                        polished = _apply_fixed_map(polished, fixed_map)
                        errors = validate_window(
                            seg_dicts, polished, speaker_map=speaker_map
                        )
                    except Exception:  # noqa: BLE001 修复失败立即走 Draft 兜底
                        pass
            if errors:
                # 修复结果仍不完整时再做一次确定性兜底，避免把可恢复窗口判失败。
                polished, errors, more_fallback = _draft_structural_fallback(
                    window, polished, draft
                )
                if more_fallback:
                    structural_fallback_ids.extend(more_fallback)
                    tr_state["structure_fallbacks"] = len(structural_fallback_ids)
            tr_state["repairing_window"] = None
            tr_state["phase"] = "finalize"
            _sync_progress(db_factory, task_id, report["steps"], "translation")
        if errors:
            polish_errors[wi] = "结构校验失败：" + "; ".join(errors[:3])
            failed_windows += 1
            report["steps"]["translation"]["failed_windows"] = failed_windows
            _sync_progress(db_factory, task_id, report["steps"], "translation")
            continue  # 不落库错误窗口；任务完成前可重试或按章节重译
        polish_errors.pop(wi, None)
        # 修复/拆窗后的文本也要再过一次确定性称谓归一，避免 repair 路径绕过。
        polished = await _canonicalize_polished_terms(
            db_factory, project_id, window, polished
        )
        with db_factory() as db:
            _persist_window_translations(db, window, draft, polished, context, model)
            db.commit()
        done += 1
        tr_state.update({
            "done": done,
            "total": total,
            "phase": "finalize",
            "draft_done": draft_done_count,
            "draft_carry": draft_carry_map,
            "finalize_done": pos + 1,
        })
        _sync_polish_live()

    if not failed_windows and not _paused():
        tr_state["phase"] = "done"
        tr_state["finalize_done"] = total
        _sync_progress(db_factory, task_id, report["steps"], "translation")

    if failed_windows and not _paused():
        errors = {
            str(wi): polish_errors.get(wi, "未知错误")
            for pos, (wi, _w) in enumerate(polish_jobs)
            if polished_results[pos] == "failed" or wi in polish_errors
        }
        report["steps"]["translation"]["failed_window_errors"] = errors
        _sync_progress(db_factory, task_id, report["steps"], "translation")
        raise RuntimeError(
            f"{failed_windows} 个翻译窗口失败；示例：{next(iter(errors.values()), '未知错误')}。"
            "可重试任务；若同一窗口持续失败，请查看任务详情或缩短该段原文。"
        )

    # 4b/5. 台词对齐与 QA 并行（都只依赖 polished 译文；写入不同表、进度 key 互不冲突）
    async def _run_align():
        from app.services import dialogue_service

        if "dialogue_align" not in report["steps"]:
            _sync_progress(db_factory, task_id, report["steps"], "dialogue_align")

            def _al_cb(p: dict) -> None:
                report["steps"]["dialogue_align"] = p
                _sync_progress(db_factory, task_id, report["steps"], "dialogue_align")

            report["steps"]["dialogue_align"] = await dialogue_service.align_dialogue_translations(
                db_factory, project_id, source_document_id, provider, model, progress_cb=_al_cb
            )
        _sync_progress(db_factory, task_id, report["steps"], "dialogue_align")

    async def _run_qa():
        _sync_progress(db_factory, task_id, report["steps"], "qa")
        # 5. QA：确定性全量 + 风险窗口语义 QA
        with db_factory() as db:
            # 全文术语强制表（含别名）：同一地点/专名的多种中文称呼映射到同一译名
            full_text = "".join(
                s.source_text
                for s in db.scalars(
                    select(SourceSegment).where(
                        SourceSegment.source_document_id == source_document_id
                    )
                )
            )
            forced = _forced_terms_for_text(db, project_id, full_text)
        prev_qa = report["steps"].get("qa") or {}
        qa_done = int(prev_qa.get("done", 0))
        issues_total = int(prev_qa.get("issues", 0))
        needs_review = int(prev_qa.get("needs_review", 0))
        blocked = int(prev_qa.get("blocked", 0))
        qa_failed = int(prev_qa.get("failed", 0))
        qa_errors: list[str] = list(prev_qa.get("errors") or [])
        semantic_failed_windows: set[int] = set()
        report["steps"]["qa"] = {"done": qa_done, "total": len(windows), "issues": issues_total}
        _sync_progress(db_factory, task_id, report["steps"], "qa")
        # 第一遍：确定性 QA（本地计算）+ 每窗口组装一次语义 QA 任务。
        # 语义 QA 看整个窗口，而不是只看“有代词/术语告警”的零散段，
        # 否则漏译、剧情缺失很容易因为没有风险触发而被跳过。
        det_by_win: dict[int, dict[str, list[dict]]] = {}
        tr_id_by_win: dict[int, dict[str, str]] = {}
        qa_jobs: list[dict] = []
        for wi, window in enumerate(windows):
            if _paused():
                break  # 暂停：保留进度
            if wi < qa_done:
                continue  # 已检查过的窗口跳过
            with db_factory() as db:
                polished_rows = list(
                    db.scalars(
                        select(Translation).where(
                            Translation.segment_id.in_([s.id for s in window]),
                            Translation.stage == "polished",
                        ).order_by(Translation.version.desc())
                    )
                )
                latest: dict[str, Translation] = {}
                hashes = {s.id: s.source_hash for s in window}
                for t in polished_rows:
                    if t.source_hash != hashes.get(t.segment_id) or t.freshness_status != "current":
                        continue
                    latest.setdefault(t.segment_id, t)
                seg_map = {s.id: s for s in window}
                _card, knowledge, storyline, states, facts = _load_window_context(
                    db, project_id, window
                )
                qa_reference = build_qa_reference(
                    segments=[
                        {
                            "segment_id": s.id,
                            "segment_type": s.segment_type,
                            "speaker_name": s.speaker_name,
                            "source_text": s.source_text,
                        }
                        for s in window
                    ],
                    knowledge=knowledge,
                    storyline=storyline,
                    character_states=states,
                    story_facts=facts,
                )
                det_by_win[wi] = {}
                tr_id_by_win[wi] = {}
                segment_payload: list[dict] = []
                for seg in window:
                    tr = latest.get(seg.id)
                    if tr is None:
                        continue
                    det = qa_service.deterministic_qa(db, seg_map[seg.id], tr, forced)
                    det_by_win[wi][seg.id] = det
                    tr_id_by_win[wi][seg.id] = tr.id
                    segment_payload.append({
                        "segment_id": seg.id,
                        "source_text": seg.source_text,
                        "target_text": tr.target_text or "",
                    })
            if segment_payload:
                source_block = "\n".join(
                    f"[{p['segment_id']}] {p['source_text']}" for p in segment_payload
                )
                target_block = "\n".join(
                    f"[{p['segment_id']}] {p['target_text']}" for p in segment_payload
                )
                qa_jobs.append({
                    "wi": wi,
                    "source_block": source_block,
                    "target_block": target_block,
                    "reference": qa_reference,
                    "segments": segment_payload,
                    "tr_id_by_sid": dict(tr_id_by_win[wi]),
                })

        def _resolve_issue_segment(issue: dict, segments: list[dict]) -> str:
            known = {p["segment_id"] for p in segments}
            sid = str(issue.get("segment_id") or "")
            if sid in known:
                return sid
            for field in ("source_quote", "target_quote"):
                quote = normalize(str(issue.get(field) or ""))
                if not quote:
                    continue
                for p in segments:
                    if quote and (
                        quote in normalize(p.get("source_text") or "")
                        or quote in normalize(p.get("target_text") or "")
                    ):
                        return p["segment_id"]
            return ""

        # 第二遍：每窗口一次语义 QA（并发，只读模型，不写库）。
        sem_by: dict[tuple[int, str], list[dict]] = {}
        nat_by_wi: dict[int, list[tuple[str, dict]]] = {}
        if qa_jobs:
            _sem_qa = _aio.Semaphore(window_concurrency)
            qa_step = report["steps"]["qa"]
            qa_step["semantic_done"] = int(qa_step.get("semantic_done") or 0)
            qa_step["semantic_total"] = len(qa_jobs)
            _sync_progress(db_factory, task_id, report["steps"], "qa")

            async def _sem_one(job):
                try:
                    async with _sem_qa:
                        result = await qa_service.semantic_qa(
                            provider, model, job["source_block"], job["target_block"],
                            reference=job["reference"],
                        )
                    if not result.ok:
                        return job["wi"], [], [], result.error or "语义质检未完成"
                    resolved: list[tuple[str, dict]] = []
                    for issue in result.issues:
                        sid = _resolve_issue_segment(issue, job["segments"])
                        tr_id = job["tr_id_by_sid"].get(sid) if sid else None
                        if tr_id:
                            issue["segment_id"] = sid
                            resolved.append((tr_id, issue))
                    natural_edits: list[tuple[str, dict]] = []
                    for edit in result.naturalness_edits:
                        sid = _resolve_issue_segment(edit, job["segments"])
                        if sid:
                            edit["segment_id"] = sid
                            natural_edits.append((sid, edit))
                    return job["wi"], resolved, natural_edits, ""
                except Exception as exc:  # noqa: BLE001 单窗口失败不阻塞其它窗口记录
                    return job["wi"], [], [], f"{type(exc).__name__}: {str(exc)[:300]}"

            sem_tasks = [_aio.create_task(_sem_one(j)) for j in qa_jobs]
            for fut in _aio.as_completed(sem_tasks):
                wi, resolved, natural_edits, error = await fut
                if error:
                    semantic_failed_windows.add(wi)
                    qa_failed += 1
                    qa_errors.append(f"WIN_{wi:04d}: {error}")
                for tr_id, issue in resolved:
                    sem_by.setdefault((wi, tr_id), []).append(issue)
                if natural_edits:
                    nat_by_wi.setdefault(wi, []).extend(natural_edits)
                qa_step["semantic_done"] = int(qa_step.get("semantic_done") or 0) + 1
                _sync_progress(db_factory, task_id, report["steps"], "qa")

        # 第三遍：顺序回填持久化 + gate 评估（单协程写，无竞争）
        severity_counts: dict[str, int] = dict(prev_qa.get("severity_counts") or {})
        naturalness_total = int(prev_qa.get("naturalness_edits") or 0)
        for wi, window in enumerate(windows):
            if wi not in det_by_win:
                continue  # 已检查过或暂停中断
            win_issues: list[dict] = []
            naturalness_count = 0
            current_job = next((j for j in qa_jobs if j["wi"] == wi), None)
            with db_factory() as db:
                if current_job is not None and nat_by_wi.get(wi):
                    exclude_sids = {
                        sid for sid, tr_id in tr_id_by_win[wi].items()
                        if (wi, tr_id) in sem_by
                    }
                    accepted = qa_service.select_naturalness_edits(
                        segments=_seg_dicts(window),
                        targets={
                            p["segment_id"]: p["target_text"]
                            for p in current_job["segments"]
                        },
                        edits=[
                            edit for _sid, edit in nat_by_wi.get(wi, [])
                        ],
                        forced_terms=forced,
                        speaker_map=speaker_map,
                        exclude_sids=exclude_sids,
                    )
                    new_rows = persist_naturalness_edits(
                        db, window=window, accepted=accepted, model=model
                    )
                    for sid, row in new_rows.items():
                        tr_id_by_win[wi][sid] = row.id
                        seg = next(s for s in window if s.id == sid)
                        det_by_win[wi][sid] = qa_service.deterministic_qa(
                            db, seg, row, forced
                        )
                    naturalness_count = len(new_rows)
                    naturalness_total += naturalness_count
                qa_service.clear_issues_for_segments(
                    db, project_id, [s.id for s in window]
                )
                for sid, tr_id in tr_id_by_win[wi].items():
                    issues = det_by_win[wi].get(sid, []) + sem_by.get((wi, tr_id), [])
                    win_issues.extend(issues)
                    if issues:
                        qa_service.persist_issues(db, project_id, tr_id, issues)
                db.commit()
            issues_total += len(win_issues)
            for issue in win_issues:
                sev = issue.get("severity", "low")
                severity_counts[sev] = severity_counts.get(sev, 0) + 1
            report["steps"]["qa"] = {
                "done": wi + 1, "total": len(windows), "issues": issues_total,
                "severity_counts": severity_counts,
                "naturalness_edits": naturalness_total,
            }
            _sync_progress(db_factory, task_id, report["steps"], "qa")
            # 质检打回定点修复：AI 只修问题段（最多 3 轮），修复段写库新版本；
            # 仍不合格转 needs_review，不整窗重做
            fixed_n, leftover = await _repair_express_qa(
                db_factory, project_id, window, win_issues, provider, model, forced
            )
            if fixed_n:
                report["steps"]["qa"]["fixed"] = int(report["steps"]["qa"].get("fixed") or 0) + fixed_n
                _sync_progress(db_factory, task_id, report["steps"], "qa")
                # 程序修复后，对原本存在语义问题的段再做一次聚焦语义复检。
                sem_targets = sorted({
                    sid for sid in (
                        i.get("segment_id") for i in win_issues
                        if i.get("issue_type") in qa_service.SEMANTIC_TYPES
                    ) if sid and sid in {s.id for s in window}
                })
                if sem_targets:
                    with db_factory() as db:
                        latest_rows = list(db.scalars(
                            select(Translation).where(
                                Translation.segment_id.in_(sem_targets),
                                Translation.stage == "polished",
                            ).order_by(Translation.version.desc())
                        ))
                        latest_map: dict[str, Translation] = {}
                        for tr in latest_rows:
                            latest_map.setdefault(tr.segment_id, tr)
                    src_block = "\n".join(
                        f"[{s.id}] {s.source_text}" for s in window if s.id in sem_targets
                    )
                    tgt_block = "\n".join(
                        f"[{sid}] {latest_map[sid].target_text}" for sid in sem_targets if sid in latest_map
                    )
                    qa_job = next((j for j in qa_jobs if j["wi"] == wi), None)
                    if src_block and tgt_block and qa_job is not None:
                        recheck = await qa_service.semantic_qa(
                            provider, model, src_block, tgt_block,
                            reference=qa_job["reference"],
                        )
                        if not recheck.ok:
                            semantic_failed_windows.add(wi)
                            qa_failed += 1
                            qa_errors.append(f"WIN_{wi:04d}: 修复后语义复检未完成：{recheck.error}")
                        else:
                            for issue in recheck.issues:
                                sid = str(issue.get("segment_id") or "")
                                if sid in sem_targets and qa_service.should_repair(issue):
                                    issue["segment_id"] = sid
                                    win_issues.append(issue)
                                    leftover = sorted(set(leftover) | {sid})
                                    with db_factory() as db:
                                        tr = latest_map.get(sid)
                                        if tr is not None:
                                            qa_service.persist_issues(db, project_id, tr.id, [issue])
            # 修复后 gate 只看未修复段与无法定位到段落的问题；有任一未通过都不允许自动定稿。
            unresolved = [
                i for i in win_issues
                if i.get("segment_id") in leftover
                or (not i.get("segment_id") and qa_service.should_repair(i))
            ]
            gate = qa_service.evaluate_gate(unresolved)
            semantic_ok = wi not in semantic_failed_windows
            if not semantic_ok:
                gate = "blocked"
            if gate == "needs_review":
                needs_review += 1
            elif gate == "blocked":
                blocked += 1
            if gate != "passed" and wi not in semantic_failed_windows:
                qa_failed += 1
            with db_factory() as db:
                latest_ids: list[str] = []
                rows = list(db.scalars(
                    select(Translation).where(
                        Translation.segment_id.in_([s.id for s in window]),
                        Translation.stage == "polished",
                    ).order_by(Translation.version.desc())
                ))
                seen: set[str] = set()
                for row in rows:
                    if row.segment_id not in seen:
                        seen.add(row.segment_id)
                        latest_ids.append(row.id)
                qa_service.replace_window_quality(
                    db, task_id, f"WIN_{wi:04d}", gate,
                    {
                        "issues": len(win_issues),
                        "unresolved": len(unresolved),
                        "semantic_ok": semantic_ok,
                        "translation_ids": latest_ids,
                        "naturalness_edits": naturalness_count,
                    },
                )
                db.commit()
        report["qa"] = {
            "issues_total": issues_total,
            "needs_review": needs_review,
            "blocked": blocked,
            "failed": qa_failed,
            "errors": qa_errors[:5],
            "gate_passed": qa_failed == 0,
            "naturalness_edits": naturalness_total,
        }
        report["steps"]["qa"] = {
            "done": len(windows), "total": len(windows),
            "issues": issues_total, "needs_review": needs_review, "blocked": blocked,
            "failed": qa_failed, "errors": qa_errors[:5],
            "gate_passed": qa_failed == 0,
            "severity_counts": report["steps"]["qa"].get("severity_counts") or {},
        }
        _sync_progress(db_factory, task_id, report["steps"], "approve")

    async def _fix_term_inconsistency():
        """术语一致性：有译名术语在出现段中未使用译名时，自动重译修正（防混用）。

        断点续跑：完成标记写入 steps 后跳过（重试不重做）；修复段并发生成、顺序落库，
        每段带硬超时，修完一段实时写进度（批准定稿前不再长时间无反馈）。
        """
        if "term_consistency" in report["steps"]:
            return  # 已完成：重试不重做
        from app.services.context_builder import ContextResult

        with db_factory() as db:
            segs_all = list(
                db.scalars(
                    select(SourceSegment).where(
                        SourceSegment.source_document_id == source_document_id
                    )
                )
            )
            # 全文术语强制表（含别名）：同一地点的多种中文称呼全部命中同一译名
            forced = _forced_terms_for_text(
                db, project_id, "".join(seg.source_text for seg in segs_all)
            )
            speaker_map = _speaker_map_for_project(db, project_id)
        seg_by_id = {seg.id: seg for seg in segs_all}
        if not forced or not segs_all:
            report["steps"]["term_consistency"] = {"fixed": 0, "found": 0}
            _sync_progress(db_factory, task_id, report["steps"], "term_consistency")
            return
        # 段级命中检测：原文含术语但最新译文未使用译名（词边界 + 大小写不敏感）
        fixes: dict[str, list[str]] = {}  # segment_id -> 译名要求说明（可多条）
        with db_factory() as db:
            for seg in segs_all:
                active = select_forced_specs_for_text(forced, seg.source_text)
                if not active:
                    continue
                t = db.scalar(
                    select(Translation)
                    .where(Translation.segment_id == seg.id, Translation.stage == "polished")
                    .order_by(Translation.version.desc())
                    .limit(1)
                )
                if t is None:
                    continue
                tgt = t.target_text or ""
                for key, spec in active.items():
                    trans = spec.get("en") or ""
                    variants = spec.get("variants") or []
                    if not trans or target_hits_term(tgt, trans, variants):
                        continue
                    fixes.setdefault(seg.id, []).append(
                        f"必须使用译名 {trans}（术语 {key}，允许变体 {variants}）"
                    )
        if not fixes:
            report["steps"]["term_consistency"] = {"fixed": 0, "found": 0}
            _sync_progress(db_factory, task_id, report["steps"], "term_consistency")
            return

        # 并发生成修复（只读）；每段硬超时，失败只跳过该段
        _sem_fix = _aio.Semaphore(window_concurrency)

        async def _fix_one(sid: str):
            seg = seg_by_id[sid]
            with db_factory() as db:
                draft_row = db.scalar(
                    select(Translation)
                    .where(Translation.segment_id == sid, Translation.stage == "draft")
                    .order_by(Translation.version.desc())
                    .limit(1)
                )
            draft_list = [
                {
                    "segment_id": sid,
                    "target_text": draft_row.target_text if draft_row else "",
                    "notes": [],
                    "ambiguity": [],
                }
            ]
            ctx = ContextResult(
                prompt_text=(
                    f"SOURCE SEGMENTS (translate only these, one translation per segment_id):\n"
                    f"[{sid}] {seg.source_text}"
                )
            )
            required = "；".join(fixes[sid])
            issues = [{"issue_type": "term", "explanation": required, "suggested_fix": required}]
            try:
                fixed_map = await _aio.wait_for(
                    repair_segments(
                        provider=provider, model=model, window_id=f"TERM-{seg.segment_no}",
                        context=ctx, draft=draft_list, issues=issues, seg_ids=[sid],
                        thinking="disabled",
                    ),
                    timeout=180,
                )
                text = fixed_map.get(sid, "")
                # 校验通过才写库（历史 bug：判定写反，导致不合格译文入库、合格译文被丢弃）
                if not text:
                    return sid, None
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
                    return sid, None
                return sid, text
            except Exception:  # noqa: BLE001 单段修复失败不阻塞
                return sid, None
            return sid, None

        results = await _aio.gather(
            *[_fix_one(sid) for sid in fixes], return_exceptions=True
        )
        fixed_count = 0
        for r in results:
            if isinstance(r, BaseException) or r is None or r[1] is None:
                continue
            sid, text = r
            with db_factory() as db:
                insert_translation_row(
                    db,
                    segment_id=sid,
                    stage="polished",
                    target_text=text,
                    review_status="unreviewed",
                    freshness_status="current",
                    notes_json="[]",
                    ambiguity_json="[]",
                    model_name=model,
                    provider_id="default",
                    prompt_version=PROMPT_VERSION,
                    context_snapshot_json="{}",
                    source_hash=sha256(seg_by_id[sid].source_text),
                )
                db.commit()
            fixed_count += 1
            report["steps"]["term_consistency"] = {"fixed": fixed_count, "found": len(fixes)}
            _sync_progress(db_factory, task_id, report["steps"], "term_consistency")
        report["steps"]["term_consistency"] = {"fixed": fixed_count, "found": len(fixes)}

    # 术语修正必须在 QA 之前完成，确保 QA 校验的是最终版本，而不是修正前的旧版本。
    await _fix_term_inconsistency()

    # QA 与台词对齐并行执行（各自硬超时：超时任务失败，重试时已完成步骤跳过）
    align_task = _aio.create_task(_run_align())
    await _aio.wait_for(_run_qa(), timeout=2400)
    await _aio.wait_for(align_task, timeout=2400)
    qa_result = report.get("qa") or {}
    from app.services.quality_service import QualityGateError

    if not qa_result.get("gate_passed"):
        reason = (
            f"{qa_result.get('failed', 0)} 个窗口仍有未完成或未解决问题，"
            f"示例：{(qa_result.get('errors') or ['存在严重问题'])[0]}"
        )
        _sync_progress(
            db_factory, task_id, report["steps"], "quality_gate",
            extra={"quality": {"gate": "blocked", "reason": reason, "auto_approved_ready": False}},
        )
        raise QualityGateError("自动质检未通过：" + reason)

    # 项目级自动门禁：结构完整性、过期译文、高严重度问题、语义质检覆盖率全部通过才定稿。
    from app.services.quality_service import auto_approve_project, project_quality_report

    with db_factory() as db:
        quality = project_quality_report(db, project_id)
        if quality.get("gate") != "passed":
            details = quality.get("structural_error_details") or []
            _sync_progress(
                db_factory, task_id, report["steps"], "quality_gate",
                extra={"quality": quality},
            )
            raise QualityGateError(
                "自动质检未通过：" + str(quality.get("reason") or "存在未解决问题")
                + (f"；示例：{details[0]}" if details else "")
            )
        approved_result = auto_approve_project(db, project_id)
    report["quality"] = quality
    report["approve"] = approved_result
    report["steps"]["approve"] = approved_result
    _sync_progress(db_factory, task_id, report["steps"], "done")
    return report
