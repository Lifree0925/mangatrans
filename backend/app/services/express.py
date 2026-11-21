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
    scene_card_service,
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
    _sync_progress(db_factory, task_id, report["steps"], "profiles")
    # 1b. 角色档案（性格/说话方式：Polish 润色依据）
    if "profiles" not in report["steps"]:
        report["steps"]["profiles"] = await profile_service.extract_profiles(
            db_factory, project_id, source_document_id, provider, model
        )
    _sync_progress(db_factory, task_id, report["steps"], "storylines")
    # 1c. 章节剧情卡（重要情节/伏笔/前情：连续一致性关键）
    if "storylines" not in report["steps"]:
        report["steps"]["storylines"] = await storyline_service.generate_for_document(
            db_factory, project_id, source_document_id, provider, model
        )
    _sync_progress(db_factory, task_id, report["steps"], "dialogue")
    # 1d. 台词本（章节→场景→角色纯对白：反推全文一致性的地基）
    if "dialogue" not in report["steps"]:
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
    _sync_progress(db_factory, task_id, report["steps"], "scene_cards")
    # 2. 场景卡
    if "scene_cards" not in report["steps"]:
        report["steps"]["scene_cards"] = await scene_card_service.generate_cards_for_document(
            db_factory, project_id, source_document_id, provider, model
        )
    _sync_progress(db_factory, task_id, report["steps"], "trial")
    # 3. 试译与风格基线
    if "trial" not in report["steps"]:
        report["steps"]["trial"] = await style_guide_service.run_trial(
            db_factory, project_id, source_document_id, provider, model
        )
    _sync_progress(db_factory, task_id, report["steps"], "translation")
    # 4. 全本窗口翻译（carry-over 串行）
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
    # 续跑：已完成窗口跳过；回退一窗重跑以重建 carry-over（仅多花一次调用）
    prev_done = int((report["steps"].get("translation") or {}).get("done", 0))
    start_win = max(0, min(prev_done - 1, total)) if prev_done > 0 else 0
    carry: dict | None = None
    done = start_win
    report["steps"]["translation"] = {"done": done, "total": total, "phase": "draft"}
    _sync_progress(db_factory, task_id, report["steps"], "translation")

    # 阶段 1：未完成窗口的 Draft 并发（Draft 不依赖 carry-over）
    todo = windows[start_win:]
    import asyncio as _aio
    _sem = _aio.Semaphore(6)

    async def _draft_one(win):
        async with _sem:
            return await translate_window_draft_direct(db_factory, project_id, win, model)

    draft_results = await _aio.gather(*[_draft_one(w) for w in todo])

    # 阶段 2：Polish 串行（carry-over 连续性底线）
    report["steps"]["translation"] = {"done": done, "total": total, "phase": "polish"}
    _sync_progress(db_factory, task_id, report["steps"], "translation")
    for wi, window in enumerate(todo, start=start_win):
        if _paused():
            break  # 暂停：保留进度，resume 续跑
        draft, context = draft_results[wi - start_win]
        polished, carry = await translate_window_polish_direct(
            db_factory, project_id, window, draft, carry, model
        )
        with db_factory() as db:
            _persist_window_translations(db, window, draft, polished, context, model)
            db.commit()
        done += 1
        report["steps"]["translation"] = {"done": done, "total": total, "phase": "polish"}
        _sync_progress(db_factory, task_id, report["steps"], "translation")

    # 4b. 台词本逐句对齐：翻译完成后把每句台词从片段译文中摘录对应英文
    if "dialogue_align" not in report["steps"]:
        _sync_progress(db_factory, task_id, report["steps"], "dialogue_align")
        from app.services import dialogue_service

        def _al_cb(p: dict) -> None:
            report["steps"]["dialogue_align"] = p
            _sync_progress(db_factory, task_id, report["steps"], "dialogue_align")

        report["steps"]["dialogue_align"] = await dialogue_service.align_dialogue_translations(
            db_factory, project_id, source_document_id, provider, model, progress_cb=_al_cb
        )
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
    for wi, window in enumerate(windows):
        if _paused():
            break  # 暂停：保留进度
        if wi < qa_done:
            continue  # 已检查过的窗口跳过
        gate = "passed"
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
            win_issues: list[dict] = []
            for sid, tr in latest.items():
                det = qa_service.deterministic_qa(db, seg_map[sid], tr, forced)
                win_issues.extend(det)
                # 风险触发语义 QA：高/严重问题或指代密集
                risky = any(
                    i["severity"] in ("high", "critical") for i in det
                ) or (
                    ("他" in seg_map[sid].source_text or "她" in seg_map[sid].source_text)
                    and sum(1 for s in window if s.speaker_name) > 1
                )
                if risky:
                    sem = await qa_service.semantic_qa(provider, model, seg_map[sid], tr)
                    win_issues.extend(sem)
                if win_issues:
                    qa_service.persist_issues(db, project_id, tr.id, win_issues)
        issues_total += len(win_issues)
        severity_counts: dict[str, int] = dict(prev_qa.get("severity_counts") or {})
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
