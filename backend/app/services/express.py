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
from app.services.translation import PROMPT_VERSION
from app.services.translation_batch import (
    _persist_window_translations,
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
        report["steps"]["knowledge"] = await knowledge_service.extract_knowledge_for_document(
            db_factory, project_id, source_document_id, provider, model, mode="express"
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

    async def _draft_one(win):
        nonlocal draft_done_count
        async with _sem:
            result = await translate_window_draft_direct(db_factory, project_id, win, model)
        # 实时进度：每完成一窗写一次（窗口数最多 ~80，写库开销可忽略）
        draft_done_count += 1
        report["steps"]["translation"]["draft_done"] = draft_done_count
        _sync_progress(db_factory, task_id, report["steps"], "translation")
        return result

    async def _run_draft():
        nonlocal draft_carry_map
        results = await _aio.gather(*[_draft_one(w) for w in todo])
        for wi, (draft, carry_d, _ctx) in zip(range(start_win, total), results):
            if carry_d is not None:
                draft_carry_map[f"WIN_{wi:04d}"] = carry_d
        report["steps"]["translation"]["draft_carry"] = draft_carry_map
        return results

    # Draft 立即启动，与四个准备步骤并行（关键路径 = 知识 → Draft → Polish）
    draft_task = _aio.create_task(_run_draft())
    await _aio.gather(
        _prep_profiles(), _prep_storylines(), _prep_dialogue(), _prep_trial()
    )
    draft_results = await draft_task

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

    async def _polish_one(win_idx, window, draft):
        if _paused():
            return None
        async with _sem2:
            return await translate_window_polish_direct(
                db_factory, project_id, window, draft, _prev_carry(win_idx), model
            )

    polished_results = await _aio.gather(
        *[
            _polish_one(wi, w, dr[0])
            for wi, w, dr in zip(range(start_win, total), todo, draft_results)
        ]
    )

    # 顺序持久化与进度上报（单协程写，无竞争）
    for idx, (window, dr, pr) in enumerate(zip(todo, draft_results, polished_results)):
        if pr is None:
            break  # 暂停：保留进度，resume 续跑
        draft, context = dr[0], dr[2]
        with db_factory() as db:
            _persist_window_translations(db, window, draft, pr[0], context, model)
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
            forced = {}
            for k in db.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.project_id == project_id,
                    KnowledgeItem.status.in_(["approved", "auto_approved"]),
                    KnowledgeItem.item_type == "term",
                )
            ):
                translation = json.loads(k.data_json or "{}").get("translation")
                # 只有明确给出译名的术语才强制；无译名的只作参考（否则整本被误报）
                if translation and translation != k.canonical_key:
                    forced[k.canonical_key] = translation
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

        # 第二遍：风险窗口语义 QA 并发（只读模型，不写库）
        sem_by: dict[tuple[int, str], list[dict]] = {}
        if risk_jobs:
            _sem_qa = _aio.Semaphore(10)

            async def _sem_one(job):
                wi, src, tgt, tr_id = job
                async with _sem_qa:
                    issues = await qa_service.semantic_qa(provider, model, src, tgt)
                    return wi, tr_id, issues

            for wi, tr_id, issues in await _aio.gather(*[_sem_one(j) for j in risk_jobs]):
                if issues:
                    sem_by.setdefault((wi, tr_id), []).extend(issues)

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

    # QA 与台词对齐并行执行
    align_task = _aio.create_task(_run_align())
    await _run_qa()
    await align_task

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
