"""术语锁定：Draft 全量完成后统一确定「无译名术语」的标准译名并写回知识库。

Polish 阶段通过 Context Builder 的 FORCED TERMS 强制使用，保证跨窗口术语一致
（历史问题：wolf queen / Luna 混用、eternal mark's kiss 三种变体）。
"""

import json

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app.llm.json_utils import parse_llm_json
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.translation import Translation

TERM_LOCK_SYSTEM = """You are a terminology editor for an English translation of a Chinese werewolf
screenplay. For each Chinese term below, decide ONE canonical English rendering.
Rules:
- Choose the most idiomatic genre convention (e.g. werewolf-fandom usage), not a literal gloss.
- Preserve capitalization conventions (e.g. Luna, Wolf King, Moon Goddess).
- Return ONLY the required JSON. No commentary."""


class TermLockOutput(BaseModel):
    terms: dict[str, str] = Field(default_factory=dict)


async def lock_untranslated_terms(
    db_factory, project_id: str, source_document_id: str, provider, model: str
) -> dict:
    """为无译名的已批准术语确定译名并写回（新版本）。返回锁定数量。"""
    with db_factory() as db:
        terms = list(
            db.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.project_id == project_id,
                    KnowledgeItem.item_type == "term",
                    KnowledgeItem.status.in_(["approved", "auto_approved"]),
                )
            )
        )
        pending = []
        for k in terms:
            data = json.loads(k.data_json or "{}")
            if not (data.get("translation") or "").strip():
                pending.append(k)
        if not pending:
            return {"locked": 0}
        # 每个待定术语的原文/草稿样本（最多 5 段）
        seg_rows = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.source_document_id == source_document_id)
                .order_by(SourceSegment.segment_no)
            )
        )
        draft_rows = list(
            db.scalars(
                select(Translation)
                .where(
                    Translation.segment_id.in_([s.id for s in seg_rows]),
                    Translation.stage == "draft",
                )
                .order_by(Translation.version.desc())
            )
        )
        latest_draft: dict[str, str] = {}
        for t in draft_rows:
            if t.segment_id not in latest_draft:
                latest_draft[t.segment_id] = t.target_text
        samples: dict[str, list[str]] = {k.canonical_key: [] for k in pending}
        for seg in seg_rows:
            for k in pending:
                if len(samples[k.canonical_key]) >= 5:
                    continue
                if k.canonical_key in seg.source_text:
                    tgt = latest_draft.get(seg.id) or ""
                    samples[k.canonical_key].append(
                        f"{seg.source_text.strip()[:160]} => {tgt.strip()[:160]}"
                    )

    # 无正文样本的术语无法定名，跳过
    candidates = {k: v for k, v in samples.items() if v}
    if not candidates:
        return {"locked": 0}

    user = json.dumps(candidates, ensure_ascii=False, indent=2)
    resp = await provider.chat(
        model=model,
        messages=[
            {"role": "system", "content": TERM_LOCK_SYSTEM},
            {"role": "user", "content": "Terms and sample context:\n" + user},
        ],
        response_schema=TermLockOutput.model_json_schema(),
        temperature=0.2,
        thinking="enabled",
        reasoning_effort="low",
    )
    try:
        out = TermLockOutput.model_validate(parse_llm_json(resp.content))
    except (ValueError, ValidationError):
        # 定名失败不阻塞：Polish 仍可进行（术语保持无译名）
        return {"locked": 0}

    locked = 0
    with db_factory() as db:
        for k in pending:
            name = out.terms.get(k.canonical_key)
            if not name or not name.strip():
                continue
            live = db.get(KnowledgeItem, k.id)
            if live is None:
                continue
            data = json.loads(live.data_json or "{}")
            if (data.get("translation") or "").strip():
                continue  # 并发/重跑保护：已有译名不动
            data["translation"] = name.strip()
            live.data_json = json.dumps(data, ensure_ascii=False)
            live.version += 1
            locked += 1
        db.commit()
    return {"locked": locked}
