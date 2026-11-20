"""知识提取：分批调用模型、Schema 校验、确定性归并、auto_approved 规则。"""

import json

from app.llm.json_utils import parse_llm_json
import re
import unicodedata

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.protocol import LLMProvider
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeAlias, KnowledgeEvidence, KnowledgeItem
from app.schemas.knowledge import ExtractionOutput

EXTRACT_PROMPT = """你是剧本分析助手。阅读给定的中文剧本片段，提取人物与术语候选。

要求：
1. 只提取原文明确出现的信息，不得补充剧情之外的背景。
2. evidence_quotes 必须是原文原句；evidence_type 只允许 explicit（原文明确）/ inferred（推断）/ uncertain（不确定）。
3. 不确定的称呼不要强行归并。
4. 只返回 JSON，不要任何解释。

片段：
{text}"""


def normalize_key(s: str) -> str:
    """Unicode 规范化：用于确定性归并。"""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


async def extract_batch(provider: LLMProvider, text: str, model: str) -> ExtractionOutput:
    prompt = EXTRACT_PROMPT.format(text=text)
    resp = await provider.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_schema=ExtractionOutput.model_json_schema(),
        temperature=0.2,
    )
    data = parse_llm_json(resp.content)
    try:
        return ExtractionOutput.model_validate(data)
    except (ValidationError, json.JSONDecodeError, AttributeError):
        # 格式异常时只保留合法条目，其余丢弃（不阻塞整批）
        if not isinstance(data, dict):
            raise
        items = [it for it in data.get("items", []) if isinstance(it, dict)]
        return ExtractionOutput.model_validate({"items": items})


def merge_candidates(
    db: Session, project_id: str, output: ExtractionOutput, mode: str
) -> dict[str, list[str]]:
    """确定性归并 + 入库。返回新知识 key -> evidence quotes。

    Express 模式：explicit 证据的 character/term 自动批准（auto_approved）；
    inferred/uncertain 保持 candidate。
    """
    existing = {
        normalize_key(k.canonical_key): k
        for k in db.scalars(
            select(KnowledgeItem).where(KnowledgeItem.project_id == project_id)
        )
    }
    added: dict[str, list[str]] = {}
    for cand in output.items:
        key = normalize_key(cand.canonical_key)
        if key in existing:
            continue  # 已存在，不重复建
        if mode == "express" and cand.evidence_type == "explicit" and cand.item_type in ("character", "term"):
            status = "auto_approved"
        else:
            status = "candidate"
        item = KnowledgeItem(
            project_id=project_id,
            item_type=cand.item_type,
            canonical_key=cand.canonical_key,
            data_json=json.dumps(cand.data, ensure_ascii=False),
            status=status,
            source="ai",
        )
        db.add(item)
        db.flush()
        for alias in cand.aliases:
            db.add(
                KnowledgeAlias(
                    knowledge_id=item.id,
                    alias=alias,
                    alias_type="variant",
                    status="candidate",
                )
            )
        for quote in cand.evidence_quotes:
            db.add(
                KnowledgeEvidence(
                    knowledge_id=item.id,
                    quote=quote,
                    evidence_type=cand.evidence_type,
                )
            )
        added[item.canonical_key] = cand.evidence_quotes
        existing[key] = item
    db.commit()
    return added


async def extract_knowledge_for_document(
    db_factory,
    project_id: str,
    source_document_id: str,
    provider: LLMProvider,
    model: str,
    mode: str,
    batch_chars: int = 3000,
) -> dict:
    """按窗口分批提取。"""
    import time

    t0 = time.perf_counter()
    with db_factory() as db:
        segs = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.source_document_id == source_document_id)
                .order_by(SourceSegment.segment_no)
            )
        )
    batches: list[str] = []
    buf = ""
    for seg in segs:
        buf += seg.source_text
        if len(buf) >= batch_chars:
            batches.append(buf)
            buf = ""
    if buf.strip():
        batches.append(buf)

    total_added = 0
    failed_batches = 0
    for i, batch in enumerate(batches):
        try:
            out = await extract_batch(provider, batch, model)
        except Exception:  # noqa: BLE001 单批失败不阻塞整本翻译
            failed_batches += 1
            continue
        with db_factory() as db:
            added = merge_candidates(db, project_id, out, mode)
        total_added += len(added)
    return {
        "batches": len(batches),
        "added": total_added,
        "failed_batches": failed_batches,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }
