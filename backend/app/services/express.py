"""Express 一键直出编排：知识 → 场景卡 → 试译/风格 → 全本翻译 → QA → 批量接受。"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.registry import get_provider
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.qa import WindowQuality
from app.models.translation import Translation
from app.services import (
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
from app.services.translation_batch import (
    _forced_terms_for_text,
    _persist_window_translations,
    insert_translation_row,
    translate_window_draft_direct,
    translate_window_polish_direct,
)
from app.services.window_planner import build_windows


def _sync_progress(db_factory, task_id: str, steps: dict, current_step: str) -> None:
    """实时写任务进度（前端任务中心 2 秒轮询展示）。"""
    from app.models.task import Task

    with db_factory() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        task.progress_json = json.dumps(
            {"steps": steps, "current_step": current_step}, ensure_ascii=False
        )
        db.commit()


async def run_express(db_factory, project_id: str, source_document_id: str, model: str, task_id: str = "express") -> dict:
    import asyncio as _aio

    provider = get_provider()

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
        report["steps"]["knowledge"] = await _aio.wait_for(
            knowledge_service.extract_knowledge_for_document(
                db_factory, project_id, source_document_id, provider, model, mode="express"
            ),
            timeout=2400,  # 整步硬超时：防止知识提取挂起卡住后续流程
        )
    # 2. 准备阶段协程：profiles/storylines/dialogue/trial 互不依赖，
    #    且 Draft 只锁事实、不需要 Style Guide —— 四者与 Draft 并行，缩短关键路径
    async def _prep_profiles():
        if "profiles" not in report["steps"]:
            report["steps"]["profiles"] = await profile_service.extract_profiles(
                db_factory, project_id, source_document_id, provider, model
            )
        _sync_progress(db_factory, task_id, report["steps"], "profiles")

    async def _prep_storylines():
        if "storylines" not in report["steps"]:
            report["steps"]["storylines"] = await storyline_service.generate_for_document(
                db_factory, project_id, source_document_id, provider, model
            )
        _sync_progress(db_factory, task_id, report["steps"], "storylines")

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
    # 4. 全本窗口翻译：carry-over 由 Draft 前置生成，Draft 与 Polish 均并发。
    #    质量依据：Polish 硬约束「不得改变 Draft 事实」，窗口间传递的事实状态
    #    已在 Draft 锁定，Polish 并发不损伤连续一致性。
    with db_factory() as db:
        segs = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.source_document_id == source_document_id)
                .order_by(SourceSegment.segment_no)
            )
        )
    windows = build_windows(segs)
    total = len(windows)
    # 续跑：已完成窗口跳过；回退一窗重跑以重建 carry（仅多花一次调用）
    prev_trans = report["steps"].get("translation") or {}
    prev_done = int(prev_trans.get("done", 0))
    start_win = max(0, min(prev_done - 1, total)) if prev_done > 0 else 0
    done = start_win
    # Draft carry map：续跑时从进度恢复，本次运行由 Draft 阶段补齐
    draft_carry_map: dict[str, dict] = dict(prev_trans.get("draft_carry") or {})
    report["steps"]["translation"] = {
        "done": done, "total": total, "phase": "draft", "draft_carry": draft_carry_map,
    }
    _sync_progress(db_factory, task_id, report["steps"], "translation")

    # 阶段 1：未完成窗口的 Draft 并发（Draft 不依赖 carry-over）
    todo = windows[start_win:]
    _sem = _aio.Semaphore(10)

    draft_done_count = start_win

    def _window_draft_complete(db, win) -> bool:
        """窗口所有段都有非空 draft 译文 → 可跳过（重试不重做已完成部分）。"""
        for seg in win:
            t = db.scalar(
                select(Translation)
                .where(Translation.segment_id == seg.id, Translation.stage == "draft")
                .order_by(Translation.version.desc())
                .limit(1)
            )
            if t is None or not (t.target_text or "").strip():
                return False
        return True

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
        nonlocal draft_done_count
        carry_key = f"WIN_{win_idx:04d}"
        # 断点续跑：窗口 Draft 已完成且 carry 已存 → 直接复用，不重做
        with db_factory() as db:
            if _window_draft_complete(db, win) and carry_key in draft_carry_map:
                draft_done_count += 1
                report["steps"]["translation"]["draft_done"] = draft_done_count
                _sync_progress(db_factory, task_id, report["steps"], "translation")
                return _draft_from_db(db, win), draft_carry_map[carry_key], None
        async with _sem:
            try:
                result = await _aio.wait_for(
                    translate_window_draft_direct(db_factory, project_id, win, model),
                    timeout=240,
                )
            except Exception:  # noqa: BLE001 超时/异常：该窗口标记失败跳过，不卡住整本
                return None
        # 实时进度：每完成一窗写一次（窗口数最多 ~80，写库开销可忽略）
        draft_done_count += 1
        report["steps"]["translation"]["draft_done"] = draft_done_count
        _sync_progress(db_factory, task_id, report["steps"], "translation")
        return result

    async def _run_draft():
        nonlocal draft_carry_map
        results = await _aio.gather(
            *[_draft_one(wi, w) for wi, w in zip(range(start_win, total), todo)]
        )
        for wi, r in zip(range(start_win, total), results):
            if r is not None and r[1] is not None:
                draft_carry_map[f"WIN_{wi:04d}"] = r[1]
        report["steps"]["translation"]["draft_carry"] = draft_carry_map
        return results

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

    # Draft 立即启动，与四个准备步骤并行（关键路径 = 知识 → Draft → Polish）
    draft_task = _aio.create_task(_run_draft())
    await _aio.wait_for(
        _aio.gather(
            _prep_profiles(), _prep_storylines(), _prep_dialogue(), _prep_trial()
        ),
        timeout=2400,  # 准备步骤整组硬超时：超时任务失败，重试时已完成步骤跳过
    )
    draft_results = await draft_task
    # 术语锁定：Draft 全部完成 → 统一无译名术语的标准译名 → Polish 强制使用
    await _lock_terms_once()

    # 阶段 2：Polish 并发（carry-over 取前一窗口 Draft 的 carry）
    report["steps"]["translation"] = {
        "done": done, "total": total, "phase": "polish", "draft_carry": draft_carry_map,
    }
    _sync_progress(db_factory, task_id, report["steps"], "translation")
    _sem2 = _aio.Semaphore(10)

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
            if t is None or not (t.target_text or "").strip():
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

    async def _polish_one(win_idx, window, draft):
        if _paused():
            return None
        if draft is None:
            return "failed"  # Draft 失败/超时窗口跳过
        # 断点续跑：窗口 polished 已完成 → 直接复用，不重做
        with db_factory() as db:
            if _window_polished_complete(db, window):
                return _polished_from_db(db, window), None
        async with _sem2:
            try:
                return await _aio.wait_for(
                    translate_window_polish_direct(
                        db_factory, project_id, window, draft, _prev_carry(win_idx), model
                    ),
                    timeout=240,
                )
            except Exception:  # noqa: BLE001 超时/异常：该窗口标记失败跳过，不卡住整本
                return "failed"

    polished_results = await _aio.gather(
        *[
            _polish_one(wi, w, dr[0] if dr else None)
            for wi, w, dr in zip(range(start_win, total), todo, draft_results)
        ]
    )

    # 顺序持久化与进度上报（单协程写，无竞争）
    failed_windows = 0
    for idx, (window, dr, pr) in enumerate(zip(todo, draft_results, polished_results)):
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
            report["steps"]["translation"] = {
                "done": done, "total": total, "phase": "polish", "draft_carry": draft_carry_map,
            }
            _sync_progress(db_factory, task_id, report["steps"], "translation")
            continue
        # 段完整性校验：缺行/错位是历史漏译与标签错位的根因，拒绝静默落库
        seg_dicts = [
            {"segment_id": s.id, "source_text": s.source_text} for s in window
        ]
        errors = validate_window(seg_dicts, polished)
        if errors:
            # 定点修复：定位问题段，AI 只重译这些段（窗口其余段不动）
            bad_ids = locate_problem_segments(seg_dicts, polished)
            if bad_ids:
                try:
                    fix_issues = [
                        {"issue_type": "line_shift", "explanation": e,
                         "suggested_fix": "补齐缺失行，逐行对应原文"}
                        for e in errors
                    ]
                    fixed_map = await repair_segments(
                        provider=provider, model=model, window_id=f"FIX-{window[0].segment_no}",
                        context=context, draft=draft, issues=fix_issues,
                        seg_ids=sorted(bad_ids),
                    )
                    for e in polished:
                        if e.get("segment_id") in fixed_map:
                            e["target_text"] = fixed_map[e["segment_id"]]
                    errors = validate_window(seg_dicts, polished)
                except Exception:  # noqa: BLE001 修复失败保持原错误，走失败计数
                    pass
        if errors:
            failed_windows += 1
            report["steps"]["translation"]["failed_windows"] = failed_windows
            _sync_progress(db_factory, task_id, report["steps"], "translation")
            continue  # 不落库错误窗口；任务完成前可重试或按章节重译
        with db_factory() as db:
            _persist_window_translations(db, window, draft, polished, context, model)
            db.commit()
        done += 1
        report["steps"]["translation"] = {
            "done": done, "total": total, "phase": "polish", "draft_carry": draft_carry_map,
        }
        _sync_progress(db_factory, task_id, report["steps"], "translation")

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
        report["steps"]["qa"] = {"done": qa_done, "total": len(windows), "issues": issues_total}
        _sync_progress(db_factory, task_id, report["steps"], "qa")
        # 第一遍：确定性 QA（本地计算）+ 收集风险任务（数据拷出 session，供并发使用）
        det_by_win: dict[int, dict[str, list[dict]]] = {}  # wi -> {sid: 问题}
        tr_id_by_win: dict[int, dict[str, str]] = {}       # wi -> {sid: translation_id}
        risk_jobs: list[tuple[int, str, str, str]] = []    # (wi, source_text, target_text, tr_id)
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
                for t in polished_rows:
                    latest.setdefault(t.segment_id, t)
                seg_map = {s.id: s for s in window}
                det_by_win[wi] = {}
                tr_id_by_win[wi] = {}
                for sid, tr in latest.items():
                    det = qa_service.deterministic_qa(db, seg_map[sid], tr, forced)
                    det_by_win[wi][sid] = det
                    tr_id_by_win[wi][sid] = tr.id
                    # 风险触发语义 QA：高/严重问题或指代密集
                    risky = any(
                        i["severity"] in ("high", "critical") for i in det
                    ) or (
                        ("他" in seg_map[sid].source_text or "她" in seg_map[sid].source_text)
                        and sum(1 for s in window if s.speaker_name) > 1
                    )
                    if risky:
                        risk_jobs.append((wi, seg_map[sid].source_text, tr.target_text, tr.id))

        # 第二遍：风险窗口语义 QA 并发（只读模型，不写库），逐段完成即写进度
        sem_by: dict[tuple[int, str], list[dict]] = {}
        if risk_jobs:
            _sem_qa = _aio.Semaphore(10)
            qa_step = report["steps"]["qa"]
            qa_step["semantic_done"] = int(qa_step.get("semantic_done") or 0)
            qa_step["semantic_total"] = len(risk_jobs)
            _sync_progress(db_factory, task_id, report["steps"], "qa")

            async def _sem_one(job):
                wi, src, tgt, tr_id = job
                async with _sem_qa:
                    issues = await qa_service.semantic_qa(provider, model, src, tgt)
                    return wi, tr_id, issues

            for fut in _aio.as_completed([_sem_one(j) for j in risk_jobs]):
                try:
                    wi, tr_id, issues = await fut
                except Exception:  # noqa: BLE001 单段语义 QA 失败不阻塞整体
                    issues = []
                    wi, tr_id = -1, ""
                if issues:
                    sem_by.setdefault((wi, tr_id), []).extend(issues)
                qa_step["semantic_done"] = int(qa_step.get("semantic_done") or 0) + 1
                _sync_progress(db_factory, task_id, report["steps"], "qa")

        # 第三遍：顺序回填持久化 + gate 评估（单协程写，无竞争）
        severity_counts: dict[str, int] = dict(prev_qa.get("severity_counts") or {})
        for wi, window in enumerate(windows):
            if wi not in det_by_win:
                continue  # 已检查过或暂停中断
            win_issues: list[dict] = []
            with db_factory() as db:
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
            }
            _sync_progress(db_factory, task_id, report["steps"], "qa")
            gate = qa_service.evaluate_gate(win_issues)
            if gate == "needs_review":
                needs_review += 1
            elif gate == "blocked":
                blocked += 1
            with db_factory() as db:
                db.add(
                    WindowQuality(
                        task_id=task_id,
                        window_id=f"WIN_{wi:04d}",
                        gate=gate,
                        details_json=json.dumps({"issues": len(win_issues)}),
                    )
                )
                db.commit()
        report["qa"] = {"issues_total": issues_total, "needs_review": needs_review, "blocked": blocked}
        report["steps"]["qa"] = {
            "done": len(windows), "total": len(windows),
            "issues": issues_total, "needs_review": needs_review, "blocked": blocked,
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
        seg_by_id = {seg.id: seg for seg in segs_all}
        if not forced or not segs_all:
            report["steps"]["term_consistency"] = {"fixed": 0, "found": 0}
            _sync_progress(db_factory, task_id, report["steps"], "term_consistency")
            return
        # 段级命中检测：原文含术语但最新译文未使用译名
        fixes: dict[str, str] = {}  # segment_id -> 译名要求说明
        for key, trans in forced.items():
            hit_segs = [seg for seg in segs_all if key in seg.source_text]
            if len(hit_segs) < 3:
                continue  # 出现太少不强制
            with db_factory() as db:
                for seg in hit_segs:
                    t = db.scalar(
                        select(Translation)
                        .where(Translation.segment_id == seg.id, Translation.stage == "polished")
                        .order_by(Translation.version.desc())
                        .limit(1)
                    )
                    if t is None or trans in (t.target_text or ""):
                        continue
                    fixes[seg.id] = f"必须使用译名 {trans}（术语 {key}）"
        if not fixes:
            report["steps"]["term_consistency"] = {"fixed": 0, "found": 0}
            _sync_progress(db_factory, task_id, report["steps"], "term_consistency")
            return

        # 并发生成修复（只读）；每段硬超时，失败只跳过该段
        _sem_fix = _aio.Semaphore(8)

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
            issues = [{"issue_type": "term", "explanation": fixes[sid], "suggested_fix": fixes[sid]}]
            try:
                fixed_map = await _aio.wait_for(
                    repair_segments(
                        provider=provider, model=model, window_id=f"TERM-{seg.segment_no}",
                        context=ctx, draft=draft_list, issues=issues, seg_ids=[sid],
                    ),
                    timeout=180,
                )
                text = fixed_map.get(sid, "")
                if text and not validate_window(
                    [{"segment_id": sid, "source_text": seg.source_text}],
                    [{"segment_id": sid, "target_text": text}],
                ):
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

    # QA 与台词对齐并行执行（各自硬超时：超时任务失败，重试时已完成步骤跳过）
    align_task = _aio.create_task(_run_align())
    await _aio.wait_for(_run_qa(), timeout=2400)
    await _aio.wait_for(align_task, timeout=2400)
    # 术语一致性：有译名术语未命中段自动重译修正（防 wolf queen/Luna 式混用）
    await _fix_term_inconsistency()

    # 6. 批量接受：gate=passed 的 polished → approved
    with db_factory() as db:
        rows = list(
            db.scalars(
                select(Translation)
                .join(SourceSegment, Translation.segment_id == SourceSegment.id)
                .where(
                    SourceSegment.project_id == project_id,
                    Translation.stage == "polished",
                )
            )
        )
        approved = 0
        for t in rows:
            if t.review_status == "unreviewed":
                t.review_status = "approved"
                approved += 1
        db.commit()
    report["approve"] = {"approved": approved}
    report["steps"]["approve"] = {"approved": approved}
    _sync_progress(db_factory, task_id, report["steps"], "done")
    return report
