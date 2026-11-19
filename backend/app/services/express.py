"""Express 一键直出编排：知识 → 场景卡 → 试译/风格 → 全本翻译 → QA → 批量接受。"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.registry import get_provider
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.qa import WindowQuality
from app.models.translation import Translation
from app.services import knowledge_service, qa_service, scene_card_service, style_guide_service
from app.services.importer import sha256
from app.services.translation import PROMPT_VERSION
from app.services.translation_batch import _persist_window_translations, translate_window_direct
from app.services.window_planner import build_windows


async def run_express(db_factory, project_id: str, source_document_id: str, model: str, task_id: str = "express") -> dict:
    provider = get_provider()
    report: dict = {"steps": {}}

    # 1. 知识提取（Express：explicit 的 character/term 自动批准）
    report["steps"]["knowledge"] = await knowledge_service.extract_knowledge_for_document(
        db_factory, project_id, source_document_id, provider, model, mode="express"
    )
    # 2. 场景卡
    report["steps"]["scene_cards"] = await scene_card_service.generate_cards_for_document(
        db_factory, project_id, source_document_id, provider, model
    )
    # 3. 试译与风格基线
    report["steps"]["trial"] = await style_guide_service.run_trial(
        db_factory, project_id, source_document_id, provider, model
    )
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
    carry: dict | None = None
    done = 0
    for wi, window in enumerate(windows):
        carry, draft, polished, context = await translate_window_direct(
            db_factory, project_id, window, carry, model
        )
        with db_factory() as db:
            _persist_window_translations(db, window, draft, polished, context, model)
            db.commit()
        done += 1
    report["steps"]["translation"] = {"windows": done}

    # 5. QA：确定性全量 + 风险窗口语义 QA
    with db_factory() as db:
        forced = {
            k.canonical_key: json.loads(k.data_json or "{}").get("translation", k.canonical_key)
            for k in db.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.project_id == project_id,
                    KnowledgeItem.status.in_(["approved", "auto_approved"]),
                    KnowledgeItem.item_type == "term",
                )
            )
        }
    issues_total = 0
    needs_review = 0
    blocked = 0
    for window in windows:
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
    return report
