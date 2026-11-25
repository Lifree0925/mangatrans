"""质量门禁失败后的定点重试：复用已完成译文，只修复已定位的问题段。"""

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import select

from app.llm.protocol import LLMProvider
from app.models.document import SourceSegment
from app.models.qa import QAIssue, WindowQuality
from app.models.translation import Translation
from app.services import qa_service
from app.services.context_builder import build_qa_reference
from app.services.importer import sha256
from app.services.term_match import normalize
from app.services.translation import PROMPT_VERSION, repair_segments, validate_window


DIRECT_RETRY_LIMIT = 2
REPAIR_TIMEOUT_SECONDS = 90


def _issue_dict(row: QAIssue) -> dict:
    confidence = float(row.confidence or 0)
    return {
        "issue_type": row.issue_type,
        "severity": row.severity,
        "source_quote": row.source_quote or "",
        "target_quote": row.target_quote or "",
        "explanation": row.explanation or "",
        "suggested_fix": row.suggested_fix or "",
        "confidence": confidence / 100.0 if confidence > 1 else confidence,
    }


def _resolve_issue_segment(issue: dict, window: list[SourceSegment], latest: dict[str, Translation]) -> str:
    known = {s.id for s in window}
    sid = str(issue.get("segment_id") or "")
    if sid in known:
        return sid
    for field in ("source_quote", "target_quote"):
        quote = normalize(str(issue.get(field) or ""))
        if not quote:
            continue
        for seg in window:
            tr = latest.get(seg.id)
            if quote and (
                quote in normalize(seg.source_text)
                or (tr is not None and quote in normalize(tr.target_text or ""))
            ):
                return seg.id
    return ""


async def retry_failed_window_quality(
    db_factory,
    *,
    provider: LLMProvider,
    model: str,
    project_id: str,
    task_id: str,
    window_id: str,
    window: list[SourceSegment],
) -> dict:
    """对上次已定位的 QA 问题做定点修复，成功后只复检修复段。

    返回 status：
    - repaired：已修复并通过聚焦复检
    - failed/blocked/give_up：定点修复失败，调用方不得再整窗重质检
    - unlocalized/stale/not_applicable：无法定点修复，调用方可走完整 QA
    """
    from app.services.translation_batch import (
        _load_window_context,
        _speaker_map_for_project,
        _window_forced_terms,
        _window_material,
        canonicalize_fixed_map,
        insert_translation_row,
    )

    with db_factory() as db:
        qrow = db.scalar(
            select(WindowQuality)
            .where(
                WindowQuality.task_id == task_id,
                WindowQuality.window_id == window_id,
            )
            .order_by(WindowQuality.created_at.desc())
            .limit(1)
        )
        if qrow is None or qrow.gate == "passed":
            return {"status": "not_applicable"}
        try:
            details = json.loads(qrow.details_json or "{}")
        except json.JSONDecodeError:
            details = {}

        rows = list(db.scalars(
            select(Translation)
            .where(
                Translation.segment_id.in_([s.id for s in window]),
                Translation.stage == "polished",
            )
            .order_by(Translation.version.desc())
        ))
        hashes = {s.id: s.source_hash for s in window}
        latest: dict[str, Translation] = {}
        for tr in rows:
            if tr.source_hash != hashes.get(tr.segment_id):
                continue
            if tr.freshness_status != "current":
                continue
            latest.setdefault(tr.segment_id, tr)
        if len(latest) != len(window):
            return {"status": "stale", "error": "缺少当前润色译文"}

        latest_ids = {tr.id for tr in latest.values()}
        quality_ids = {str(x) for x in (details.get("translation_ids") or []) if x}
        if quality_ids and quality_ids != latest_ids:
            return {"status": "stale", "error": "质检快照与当前译文不一致"}

        issue_rows = list(db.scalars(
            select(QAIssue).where(
                QAIssue.project_id == project_id,
                QAIssue.translation_id.in_(sorted(latest_ids)),
            )
        ))
        tr_to_sid = {tr.id: sid for sid, tr in latest.items()}
        issues: list[dict] = []
        seen: set[tuple] = set()
        for row in issue_rows:
            sid = tr_to_sid.get(row.translation_id or "")
            if not sid:
                continue
            issue = _issue_dict(row)
            if not qa_service.should_repair(issue):
                continue
            key = (
                sid, issue["issue_type"], issue["severity"],
                issue["source_quote"], issue["target_quote"], issue["explanation"],
            )
            if key in seen:
                continue
            seen.add(key)
            issue["segment_id"] = sid
            issues.append(issue)
        if not issues:
            return {"status": "unlocalized", "error": "没有可定位的未解决问题"}

        attempts = int(details.get("direct_retry_attempts") or 0)
        if attempts >= DIRECT_RETRY_LIMIT:
            return {
                "status": "give_up",
                "error": f"该窗口已定点修复 {attempts} 次仍未通过，请重新翻译该章节或人工处理",
            }
        attempts += 1
        # 先落尝试次数：即使进程中途退出，也不会无限重复定点修复。
        details["direct_retry_attempts"] = attempts
        qrow.details_json = json.dumps(details, ensure_ascii=False)
        db.commit()

        forced = _window_forced_terms(db, project_id, window)
        speaker_map = _speaker_map_for_project(db, project_id)
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

    seg_by_id = {s.id: s for s in window}
    target_ids = sorted({i["segment_id"] for i in issues if i.get("segment_id") in seg_by_id})
    if not target_ids:
        return {"status": "unlocalized", "error": "问题无法定位到片段"}

    with db_factory() as db:
        draft_rows = list(db.scalars(
            select(Translation)
            .where(
                Translation.segment_id.in_(target_ids),
                Translation.stage == "draft",
            )
            .order_by(Translation.version.desc())
        ))
        draft_latest: dict[str, Translation] = {}
        for tr in draft_rows:
            if tr.source_hash == hashes.get(tr.segment_id) and tr.freshness_status == "current":
                draft_latest.setdefault(tr.segment_id, tr)
    draft = [
        {
            "segment_id": sid,
            "target_text": (
                draft_latest[sid].target_text if sid in draft_latest
                else latest[sid].target_text
            ),
        }
        for sid in target_ids
    ]

    _sd, context, _sg, _card = await _window_material(
        db_factory, project_id, window, None
    )
    remaining = list(target_ids)
    fixed_total = 0
    for _round in range(3):
        if not remaining:
            break
        issue_payload = [i for i in issues if i.get("segment_id") in remaining]
        has_high = any(i.get("severity") in ("high", "critical") for i in issue_payload)
        try:
            fixed_map = await asyncio.wait_for(
                repair_segments(
                    provider=provider,
                    model=model,
                    window_id=f"QA-RETRY-{window_id}",
                    context=context,
                    draft=[d for d in draft if d["segment_id"] in remaining],
                    issues=issue_payload,
                    seg_ids=remaining,
                    thinking="enabled" if has_high else "disabled",
                ),
                timeout=REPAIR_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 修复失败后保留原译文并显式失败
            fixed_map = {}
        if not fixed_map:
            break
        fixed_map = await canonicalize_fixed_map(
            db_factory, project_id, window, fixed_map
        )

        fixed_ok: dict[str, str] = {}
        for sid in remaining:
            text = str(fixed_map.get(sid) or "").strip()
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
            break

        with db_factory() as db:
            for sid, text in fixed_ok.items():
                insert_translation_row(
                    db,
                    segment_id=sid,
                    stage="polished",
                    target_text=text,
                    review_status="unreviewed",
                    freshness_status="current",
                    notes_json='["qa-direct-retry"]',
                    ambiguity_json="[]",
                    model_name=model,
                    provider_id="default",
                    prompt_version=PROMPT_VERSION,
                    context_snapshot_json=json.dumps(context.snapshot, ensure_ascii=False),
                    source_hash=sha256(seg_by_id[sid].source_text),
                )
        fixed_total += len(fixed_ok)

        with db_factory() as db:
            still_bad: list[str] = [sid for sid in remaining if sid not in fixed_ok]
            for sid in fixed_ok:
                det = qa_service.deterministic_qa(
                    db, seg_by_id[sid],
                    SimpleNamespace(target_text=fixed_ok[sid]), forced,
                )
                if any(qa_service.should_repair(i) for i in det):
                    still_bad.append(sid)
        remaining = still_bad

    if remaining:
        _mark_retry_failure(
            db_factory, project_id, task_id, window_id, window,
            issues=[i for i in issues if i.get("segment_id") in remaining],
            gate="needs_review", semantic_ok=False, attempts=attempts,
        )
        return {
            "status": "failed",
            "error": f"定点修复未通过：{len(remaining)} 个问题段仍需处理",
            "repaired": fixed_total,
            "remaining": remaining,
        }

    with db_factory() as db:
        latest_rows = list(db.scalars(
            select(Translation)
            .where(
                Translation.segment_id.in_([s.id for s in window]),
                Translation.stage == "polished",
            )
            .order_by(Translation.version.desc())
        ))
        latest_now: dict[str, Translation] = {}
        for tr in latest_rows:
            if tr.source_hash != hashes.get(tr.segment_id):
                continue
            if tr.freshness_status != "current":
                continue
            latest_now.setdefault(tr.segment_id, tr)
    target_segs = [s for s in window if s.id in set(target_ids)]
    source_block = "\n".join(f"[{s.id}] {s.source_text}" for s in target_segs)
    target_block = "\n".join(
        f"[{s.id}] {latest_now[s.id].target_text}"
        for s in target_segs if s.id in latest_now
    )
    if any(s.id not in latest_now for s in target_segs):
        _mark_retry_failure(
            db_factory, project_id, task_id, window_id, window,
            issues=issues, gate="needs_review", semantic_ok=False, attempts=attempts,
        )
        return {"status": "failed", "error": "定点修复后缺少当前译文"}
    coverage_hint, _coverage = qa_service.build_coverage_signals(
        [{"segment_id": s.id, "source_text": s.source_text} for s in target_segs],
        {s.id: latest_now[s.id].target_text for s in target_segs if s.id in latest_now},
    )
    recheck = await qa_service.semantic_qa(
        provider, model, source_block, target_block,
        reference=qa_reference, coverage=coverage_hint,
    )
    if not recheck.ok:
        _mark_retry_failure(
            db_factory, project_id, task_id, window_id, window,
            issues=issues, gate="blocked", semantic_ok=False, attempts=attempts,
        )
        return {"status": "blocked", "error": f"定点修复后复检未完成：{recheck.error}"}

    recheck_issues: list[dict] = []
    for issue in recheck.issues:
        sid = _resolve_issue_segment(issue, window, latest_now)
        if not sid:
            continue
        issue["segment_id"] = sid
        if qa_service.should_repair(issue):
            recheck_issues.append(issue)
    if recheck_issues:
        _mark_retry_failure(
            db_factory, project_id, task_id, window_id, window,
            issues=recheck_issues, gate="needs_review", semantic_ok=True, attempts=attempts,
        )
        return {
            "status": "failed",
            "error": f"定点修复后复检仍有 {len(recheck_issues)} 个问题",
            "repaired": fixed_total,
            "remaining": sorted({i["segment_id"] for i in recheck_issues}),
        }

    with db_factory() as db:
        if issues:
            qa_service.clear_issues_for_segments(db, project_id, [s.id for s in window])
        latest_ids = [
            latest_now[s.id].id for s in window if s.id in latest_now
        ]
        qa_service.replace_window_quality(
            db, task_id, window_id, "passed",
            {
                "semantic_ok": True,
                "issues": 0,
                "unresolved": 0,
                "repaired": fixed_total,
                "direct_retry_attempts": attempts,
                "translation_ids": latest_ids,
            },
        )
        db.commit()
    return {"status": "repaired", "repaired": fixed_total, "window_id": window_id}


def _mark_retry_failure(
    db_factory, project_id: str, task_id: str, window_id: str,
    window: list[SourceSegment], *, issues: list[dict], gate: str,
    semantic_ok: bool, attempts: int,
) -> None:
    """保留定点重试失败现场，供下一次重试继续使用。"""
    with db_factory() as db:
        latest_rows = list(db.scalars(
            select(Translation)
            .where(
                Translation.segment_id.in_([s.id for s in window]),
                Translation.stage == "polished",
            )
            .order_by(Translation.version.desc())
        ))
        hashes = {s.id: s.source_hash for s in window}
        latest: dict[str, Translation] = {}
        for tr in latest_rows:
            if tr.source_hash != hashes.get(tr.segment_id):
                continue
            if tr.freshness_status != "current":
                continue
            latest.setdefault(tr.segment_id, tr)
        if issues:
            qa_service.clear_issues_for_segments(db, project_id, [s.id for s in window])
        by_sid: dict[str, list[dict]] = {}
        for issue in issues:
            sid = str(issue.get("segment_id") or "")
            if sid:
                by_sid.setdefault(sid, []).append(issue)
        for sid, rows in by_sid.items():
            tr = latest.get(sid)
            if tr is not None:
                qa_service.persist_issues(db, project_id, tr.id, rows)
        qa_service.replace_window_quality(
            db, task_id, window_id, gate,
            {
                "semantic_ok": semantic_ok,
                "issues": len(issues),
                "unresolved": len(by_sid),
                "direct_retry_attempts": attempts,
                "translation_ids": [tr.id for tr in latest.values()],
            },
        )
        db.commit()
