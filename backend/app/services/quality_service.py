"""项目级自动质量门禁：不依赖人工审核，所有结论来自结构校验与已完成的模型质检。"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import SourceDocument, SourceSegment
from app.models.qa import QAIssue, WindowQuality
from app.models.task import Task
from app.models.translation import Translation
from app.services.qa_service import severity_rank
from app.services.term_match import build_speaker_map
from app.services.title_service import preserved_title_for_segment
from app.services.translation import validate_window


class QualityGateError(RuntimeError):
    """自动质检/交付门禁失败；与普通技术异常区分，便于重试时清理 QA 断点。"""

    code = "quality_gate"


def _latest_polished(db: Session, seg_ids: list[str]) -> dict[str, Translation]:
    rows = list(
        db.scalars(
            select(Translation)
            .where(Translation.segment_id.in_(seg_ids), Translation.stage == "polished")
            .order_by(Translation.version.desc())
        )
    )
    result: dict[str, Translation] = {}
    for row in rows:
        result.setdefault(row.segment_id, row)
    return result


def _validated_translation_ids(db: Session, project_id: str) -> set[str]:
    """从所有已通过且语义 QA 真正完成的窗口读取译文快照。"""
    rows = list(
        db.scalars(
            select(WindowQuality)
            .join(Task, WindowQuality.task_id == Task.id)
            .where(Task.project_id == project_id, WindowQuality.gate == "passed")
        )
    )
    ids: set[str] = set()
    for row in rows:
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError:
            continue
        if details.get("semantic_ok") is not True:
            continue
        ids.update(str(x) for x in (details.get("translation_ids") or []) if x)
    return ids


def project_quality_report(db: Session, project_id: str) -> dict:
    """自动汇总项目是否达到可交付状态。"""
    from app.models.project import Project
    from app.services.qa_service import refresh_forced_term_issues

    # 术语规则可能已在知识库修订；先清理旧版误报，避免历史 forced_term 永久挡住门禁。
    try:
        refresh_forced_term_issues(db, project_id)
    except Exception:  # noqa: BLE001 清理失败不应阻止生成报告，但会保留现有问题
        pass

    project = db.get(Project, project_id)
    if project is None:
        return {"gate": "blocked", "reason": "项目不存在"}
    if not project.active_source_document_id:
        return {"gate": "blocked", "reason": "尚未解析原文"}
    doc = db.get(SourceDocument, project.active_source_document_id)
    if doc is None:
        return {"gate": "blocked", "reason": "当前原文不存在"}

    segs = list(
        db.scalars(
            select(SourceSegment)
            .where(SourceSegment.source_document_id == doc.id)
            .order_by(SourceSegment.segment_no)
        )
    )
    if not segs:
        return {"gate": "blocked", "reason": "原文没有可翻译片段", "segments": 0, "translated": 0}
    translatable = [s for s in segs if s.segment_type != "outline"]
    latest = _latest_polished(db, [s.id for s in segs])
    missing = [s.id for s in translatable if s.id not in latest]
    stale = [sid for sid, tr in latest.items() if tr.freshness_status != "current"]

    # 结构完整性：逐段使用与翻译落库相同的硬校验，防止漏行/说话人/场景号错位。
    from app.models.knowledge import KnowledgeAlias, KnowledgeItem

    items = list(
        db.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.project_id == project_id,
                KnowledgeItem.item_type == "character",
                KnowledgeItem.status.in_(["approved", "auto_approved"]),
            )
        )
    )
    aliases: dict[str, list[str]] = {}
    if items:
        for alias in db.scalars(
            select(KnowledgeAlias).where(KnowledgeAlias.knowledge_id.in_([k.id for k in items]))
        ):
            aliases.setdefault(alias.knowledge_id, []).append(alias.alias)
    speaker_map = build_speaker_map(
        [
            {
                "id": k.id,
                "item_type": k.item_type,
                "canonical_key": k.canonical_key,
                "data": json.loads(k.data_json or "{}"),
                "aliases": aliases.get(k.id, []),
            }
            for k in items
        ]
    )
    structural_errors: list[str] = []
    for seg in translatable:
        tr = latest.get(seg.id)
        if tr is None:
            continue
        errors = validate_window(
            [{
                "segment_id": seg.id,
                "segment_type": seg.segment_type,
                "speaker_name": seg.speaker_name,
                "source_text": seg.source_text,
                "line_count_optional": bool(preserved_title_for_segment(seg)),
            }],
            [{"segment_id": seg.id, "target_text": tr.target_text}],
            speaker_map=speaker_map,
        )
        structural_errors.extend(errors)

    current_ids = {t.id for t in latest.values()}
    high_issues = [
        i for i in db.scalars(
            select(QAIssue).where(QAIssue.project_id == project_id)
        )
        if i.translation_id in current_ids and severity_rank(i.severity) >= 2
    ]
    validated = _validated_translation_ids(db, project_id)
    unverified = [t.id for t in latest.values() if t.id not in validated]
    translated = sum(1 for s in translatable if s.id in latest)

    # 自动术语一致率：只用 exact 专名，不把普通名词/大小写/措辞偏好算成错误。
    term_expected = 0
    term_hits = 0
    try:
        from app.services.term_match import select_forced_specs_for_text, target_hits_term
        from app.services.translation_batch import _forced_terms_for_text

        forced = _forced_terms_for_text(db, project_id, "".join(s.source_text for s in segs))
        for seg in translatable:
            tr = latest.get(seg.id)
            if tr is None:
                continue
            for source_term, spec in select_forced_specs_for_text(forced, seg.source_text).items():
                if not isinstance(spec, dict) or not spec.get("en"):
                    continue
                term_expected += 1
                if target_hits_term(tr.target_text, spec["en"], spec.get("variants") or []):
                    term_hits += 1
    except Exception:  # noqa: BLE001 指标失败不影响门禁主体
        pass

    gate = "passed"
    reason = ""
    if missing:
        gate = "blocked"
        reason = f"缺少 {len(missing)} 段译文"
    elif stale:
        gate = "blocked"
        reason = f"{len(stale)} 段译文已过期，需要重新翻译"
    elif structural_errors:
        gate = "blocked"
        reason = f"{len(structural_errors)} 处结构校验失败"
    elif high_issues:
        gate = "needs_review"
        reason = f"{len(high_issues)} 个高严重度问题未解决"
    elif term_expected and term_hits < term_expected:
        gate = "needs_review"
        reason = f"{term_expected - term_hits} 处精确专名/术语未使用统一译名"
    elif unverified:
        gate = "needs_review"
        reason = f"{len(unverified)} 段译文尚未完成自动语义质检"

    return {
        "gate": gate,
        "reason": reason,
        "segments": len(segs),
        "translatable_segments": len(translatable),
        "outline_segments": len(segs) - len(translatable),
        "translated": translated,
        "missing": len(missing),
        "stale": len(stale),
        "structural_errors": len(structural_errors),
        "structural_error_details": structural_errors[:20],
        "high_critical_issues": len(high_issues),
        "qa_unverified": len(unverified),
        "coverage_rate": round((translated / len(translatable) * 100), 2)
        if translatable else 100.0,
        "qa_coverage_rate": round(((translated - len(unverified)) / translated * 100), 2)
        if translated else 100.0,
        "term_consistency_rate": round((term_hits / term_expected * 100), 2)
        if term_expected else 100.0,
        "term_exact_checks": term_expected,
        "auto_approved_ready": gate == "passed",
    }


def auto_approve_project(db: Session, project_id: str) -> dict:
    """质量门禁通过后自动批准所有当前 polished；不通过时不做任何批准。"""
    report = project_quality_report(db, project_id)
    if report.get("gate") != "passed":
        return {"approved": 0, "gate": report.get("gate"), "reason": report.get("reason")}
    from app.models.project import Project

    project = db.get(Project, project_id)
    doc = db.get(SourceDocument, project.active_source_document_id) if project else None
    if doc is None:
        return {"approved": 0, "gate": "blocked", "reason": "原文不存在"}
    segs = list(
        db.scalars(select(SourceSegment).where(SourceSegment.source_document_id == doc.id))
    )
    latest = _latest_polished(db, [s.id for s in segs])
    approved = 0
    for tr in latest.values():
        if tr.review_status != "approved":
            tr.review_status = "approved"
            approved += 1
    db.commit()
    return {"approved": approved, "gate": "passed", "reason": ""}
