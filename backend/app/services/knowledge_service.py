"""知识提取：并发调用模型、Schema 校验、串行确定性归并、术语治理。

历史根因：
- 8 路并发各自读旧快照后写库，可能并发创建同一实体；
- 模型把「庄园/大厅/祭坛/酒吧」这类泛词挂成具体地点的别名；
- 多个实体共享同一泛别名，后写覆盖先写，强制术语表与 QA 随之全错。

现在的约束：
- 模型调用可并发，写库与归并严格串行；
- 泛名词条不进入知识库，泛别名词不落库；
- 同一项目内同一实体只保留一个 canonical 条目；
- 共享/冲突别名在落库后确定性清理。
"""

import asyncio
import json
import re
import unicodedata

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.json_utils import parse_llm_json
from app.llm.protocol import LLMProvider
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeAlias, KnowledgeEvidence, KnowledgeItem
from app.schemas.knowledge import ExtractionOutput
from app.services.term_match import (
    classify_term, enforcement_for, is_address_form, is_descriptive_place,
    is_generic_source, normalize,
)

EXTRACT_PROMPT = """你是剧本分析助手。任务：提取人物、术语与显式人物关系候选，并治理具名实体与跨章节设定术语。
阅读给定的中文剧本片段，提取真正需要全文一致或影响人物理解的知识。

只提取真正需要全文一致或影响人物关系理解的内容，严格避免把普通词当专名。

必须遵守：
1. 只提取原文明确出现的信息；不得补充剧情之外的背景。
2. evidence_quotes 必须是原文原句；evidence_type 只允许 explicit（原文明确）/ inferred（推断）/ uncertain（不确定）。
3. 不要提取普通名词、类别词或泛称，例如：
   庄园、大厅、宴会厅、祭坛、前院、大门、二楼、地牢、墓园、修道院、树林、酒吧、房间、
   地下室、山洞、走廊、餐厅、血脉、银器、银剑、手铐、匕首、徽章、通讯器、狼群、狼人、
   吸血鬼、贵族、卫兵、护卫、守卫、仆人、侍女、医生、刺客、信使、战士、长老、头目、管家。
   这些词必须根据当前句子的语境自然翻译，不能绑定到某一个具体地点或实体。
4. 需要提取的内容：
   - 人物角色；
   - 人物身份/职业（原文明确时写入该 character 的 data.role；英文职业名写入 data.role_translation）；
   - 具名地点（例：荒原边缘酒吧、Valentine Manor、Van Helsing家族庄园）；
   - 头衔/世界设定（例：狼王、狼后、阿尔法、月亮女神、猎魔人、命定伴侣、永恒标记）；
   - 反复出现且存在专名性质的物件/组织（例：禁魔手铐、Van Helsing家族徽章）；
   - 反复出现、剧情关键且需要统一称谓的设定物（例：特定神经毒素、病毒、实验项目、武器、
     组织或契约）。只有原文把它当作固定设定名时才提取，普通药物/普通武器不提取；
   - 原文明确说明的人物关系（母亲、父亲、姐妹、夫妻、上下级、律师、公关总监等）。
5. 地点必须使用完整名称作为 canonical_key，不能把「酒吧」「庄园」「大厅」单独作为地点名。
   同一地点有正式全名和简称时，canonical_key 用最完整的名称，简称只放 aliases，
   且简称必须仍然带有区分性（例如「荒原边缘酒吧」alias 可含「边缘酒吧」，但绝不能含「酒吧」）。
6. 每个 character 的 data 必须包含 translation：角色的标准英文名；如果原文名本身是英文则原样填写。
   原文没有明确身份/职业时不要猜；有明确身份/职业时，同时给出 data.role（中文）与
   data.role_translation（自然英文职位/身份，不是逐字拼词）。
7. 每个 term 的 data 必须包含：
   - translation：唯一标准英文译名；
   - variants：合法英文词形（复数/所有格等），没有则 []；
   - term_kind：proper_name / title / place / item / common_noun / species / concept 之一；
   - enforcement：proper_name/title/place/item 用 exact；common_noun/species/concept 用 contextual。
   对剧情关键的固定设定物，如果原文反复使用同一名称且换词会改变指代，使用 exact；普通类别词仍用 contextual。
8. aliases 只允许是“同一实体的另一个完整称呼”，不得放普通类别词、代词、亲属称谓或自称。
9. 不确定是否同一实体时，宁可不合并，也不要制造错误别名。
10. 人物关系必须是显式证据；输出 item_type=relation，canonical_key 用「人物A—人物B」，
    data 至少包含 subject（A）、object（B）、relation（中文关系）、relation_translation（自然英文关系）。
    没有明确证据时不要输出，不能根据年龄、性别或称呼猜亲属关系。
11. 只返回 JSON，不要任何解释。

片段：
{text}"""


def normalize_key(s: str) -> str:
    """Unicode 规范化：用于确定性归并。"""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _candidate_data(cand) -> dict:
    data = dict(cand.data or {})
    if cand.item_type == "term":
        canonical = cand.canonical_key.strip()
        data["translation"] = str(data.get("translation") or "").strip()
        variants = data.get("variants") or []
        data["variants"] = list(dict.fromkeys(
            str(v).strip() for v in variants if str(v).strip()
        )) if isinstance(variants, list) else []
        kind = str(data.get("term_kind") or "").strip().lower()
        if not kind:
            kind = classify_term(canonical, data)
        data["term_kind"] = kind
        policy = str(data.get("enforcement") or "").strip().lower()
        if policy not in ("exact", "lexical", "contextual"):
            policy = enforcement_for(canonical, data)
        data["enforcement"] = policy
    return data


def _auto_approved(mode: str, cand, data: dict) -> bool:
    if mode != "express" or cand.evidence_type != "explicit":
        return False
    # 自动批准只接受更高置信度；低置信度仍可在知识库中作为候选编辑。
    if float(getattr(cand, "confidence", 0) or 0) < 0.85:
        return False
    if cand.item_type == "term":
        if is_generic_source(cand.canonical_key):
            return False
        if is_descriptive_place(cand.canonical_key, str(data.get("term_kind") or "")):
            return False
        if data.get("translation_conflicts"):
            return False
        if not data.get("translation"):
            return False
        return data.get("enforcement") == "exact"
    return cand.item_type == "character"


def _merge_data(existing_json: str, incoming: dict) -> str:
    """合并知识 data：保留人工/首次确定的事实，只补空字段与合法变体。"""
    merged = json.loads(existing_json or "{}")
    for key, value in incoming.items():
        if key == "translation":
            existing = str(merged.get("translation") or "").strip()
            incoming_translation = str(value or "").strip()
            if not existing and incoming_translation:
                merged["translation"] = incoming_translation
            elif existing and incoming_translation and normalize(existing) != normalize(incoming_translation):
                conflicts = list(merged.get("translation_conflicts") or [])
                if incoming_translation not in conflicts:
                    conflicts.append(incoming_translation)
                merged["translation_conflicts"] = conflicts
                # 译名冲突时降级为情境参考，禁止错误/未确认译名被逐字强制。
                merged["enforcement"] = "contextual"
            continue
        if key == "variants":
            vals = list(merged.get("variants") or [])
            for item in value or []:
                if item and item not in vals:
                    vals.append(item)
            merged["variants"] = vals
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return json.dumps(merged, ensure_ascii=False)


def _alias_allowed(
    alias: str, canonical: str, item_type: str, source_text: str,
    alias_owners: dict[str, set[str]], item_id: str,
) -> bool:
    alias = (alias or "").strip()
    if not alias or normalize(alias) == normalize(canonical):
        return False
    if len(alias) <= 1:
        return False
    if item_type == "term" and is_generic_source(alias):
        return False
    # 模型声称的别名必须在当前片段里真实出现，避免跨批臆造。
    if source_text and alias not in source_text:
        return False
    owners = alias_owners.get(normalize(alias), set())
    return not owners or owners == {item_id}


def merge_candidates(
    db: Session, project_id: str, output: ExtractionOutput, mode: str,
    source_text: str = "",
) -> dict[str, list[str]]:
    """确定性归并 + 入库；同一 canonical 只更新，不重复创建。"""
    existing_items = list(
        db.scalars(select(KnowledgeItem).where(KnowledgeItem.project_id == project_id))
    )
    existing: dict[tuple[str, str], KnowledgeItem] = {
        (k.item_type, normalize_key(k.canonical_key)): k for k in existing_items
    }
    canonical_owner: dict[str, str] = {
        normalize(k.canonical_key): k.id for k in existing_items
    }
    project_item_ids = [k.id for k in existing_items]
    alias_rows = list(db.scalars(
        select(KnowledgeAlias).where(KnowledgeAlias.knowledge_id.in_(project_item_ids))
    )) if project_item_ids else []
    alias_owners: dict[str, set[str]] = {}
    for a in alias_rows:
        alias_owners.setdefault(normalize(a.alias), set()).add(a.knowledge_id)

    added: dict[str, list[str]] = {}
    for cand in output.items:
        canonical = cand.canonical_key.strip()
        if not canonical:
            continue
        if cand.item_type == "term" and (
            is_generic_source(canonical) or is_address_form(canonical)
        ):
            # 泛词和人物称谓不进入精确术语库；称谓必须按当前语境生成。
            continue
        data = _candidate_data(cand)
        key = (cand.item_type, normalize_key(canonical))
        item = existing.get(key)
        if item is None:
            status = "auto_approved" if _auto_approved(mode, cand, data) else "candidate"
            item = KnowledgeItem(
                project_id=project_id,
                item_type=cand.item_type,
                canonical_key=canonical,
                data_json=json.dumps(data, ensure_ascii=False),
                status=status,
                source="ai",
            )
            db.add(item)
            db.flush()
            existing[key] = item
            canonical_owner[normalize_key(canonical)] = item.id
        else:
            item.data_json = _merge_data(item.data_json, data)
            item.version += 1
            merged_data = json.loads(item.data_json or "{}")
            if (
                not merged_data.get("translation_conflicts")
                and _auto_approved(mode, cand, data)
                and item.status == "candidate"
            ):
                item.status = "auto_approved"

        for alias in cand.aliases:
            if not _alias_allowed(
                alias, item.canonical_key, item.item_type, source_text,
                alias_owners, item.id,
            ):
                continue
            alias_norm = normalize(alias)
            # alias 与另一个条目的 canonical 同名：不落库，避免语义冲突。
            other_id = canonical_owner.get(alias_norm)
            if other_id and other_id != item.id:
                continue
            exists = db.scalar(
                select(KnowledgeAlias.id).where(
                    KnowledgeAlias.knowledge_id == item.id,
                    KnowledgeAlias.alias == alias,
                ).limit(1)
            )
            if exists is None:
                db.add(KnowledgeAlias(
                    knowledge_id=item.id,
                    alias=alias,
                    alias_type="variant",
                    status="approved" if item.status == "auto_approved" else "candidate",
                ))
                alias_owners.setdefault(alias_norm, set()).add(item.id)

        for quote in cand.evidence_quotes:
            quote = (quote or "").strip()
            if not quote:
                continue
            exists = db.scalar(
                select(KnowledgeEvidence.id).where(
                    KnowledgeEvidence.knowledge_id == item.id,
                    KnowledgeEvidence.quote == quote,
                ).limit(1)
            )
            if exists is None:
                db.add(KnowledgeEvidence(
                    knowledge_id=item.id,
                    quote=quote,
                    evidence_type=cand.evidence_type,
                ))
        added[item.canonical_key] = cand.evidence_quotes
    db.commit()
    return added


def prune_ambiguous_aliases(db: Session, project_id: str) -> int:
    """清理泛别名、共享别名和与其它 canonical 冲突的别名。"""
    items = {
        k.id: k for k in db.scalars(
            select(KnowledgeItem).where(KnowledgeItem.project_id == project_id)
        )
    }
    if not items:
        return 0
    aliases = list(db.scalars(
        select(KnowledgeAlias).where(KnowledgeAlias.knowledge_id.in_(items))
    ))
    owners: dict[str, list[KnowledgeAlias]] = {}
    for a in aliases:
        owners.setdefault(normalize(a.alias), []).append(a)
    canonical_ids = {normalize(k.canonical_key): k.id for k in items.values()}
    removed = 0
    for alias_norm, rows in owners.items():
        owner_ids = {r.knowledge_id for r in rows}
        canonical_owner = canonical_ids.get(alias_norm)
        keep_id: str | None = None
        if not is_generic_source(rows[0].alias):
            if canonical_owner and len(owner_ids) == 1:
                keep_id = canonical_owner
            elif len(owner_ids) == 1 and canonical_owner is None:
                keep_id = next(iter(owner_ids))
            elif canonical_owner:
                keep_id = canonical_owner
        for row in rows:
            if keep_id is not None and row.knowledge_id == keep_id:
                continue
            db.delete(row)
            removed += 1
    db.commit()
    return removed


async def extract_batch(provider: LLMProvider, text: str, model: str) -> ExtractionOutput:
    prompt = EXTRACT_PROMPT.format(text=text)
    resp = await provider.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_schema=ExtractionOutput.model_json_schema(),
        temperature=0.2,
        stage="knowledge",
    )
    data = parse_llm_json(resp.content)
    try:
        return ExtractionOutput.model_validate(data)
    except (ValidationError, json.JSONDecodeError, AttributeError):
        if not isinstance(data, dict):
            raise
        items = [it for it in data.get("items", []) if isinstance(it, dict)]
        return ExtractionOutput.model_validate({"items": items})


async def extract_knowledge_for_document(
    db_factory,
    project_id: str,
    source_document_id: str,
    provider: LLMProvider,
    model: str,
    mode: str,
    batch_chars: int = 3000,
) -> dict:
    """并发调用抽取，等待全部返回后串行归并。

    这样既保留抽取吞吐，又不会出现多批同时创建同一知识条目的竞态。
    """
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

    async def one_batch(batch: str):
        last_err = ""
        for attempt in range(2):
            try:
                return batch, await extract_batch(provider, batch, model), last_err
            except Exception as exc:  # noqa: BLE001 单批重试一次
                last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
                if attempt == 0:
                    await asyncio.sleep(1.5)
        return batch, None, last_err

    sem = asyncio.Semaphore(8)

    async def limited(batch: str):
        async with sem:
            return await one_batch(batch)

    results = await asyncio.gather(*(limited(b) for b in batches))
    total_added = 0
    failed_batches = 0
    errors: list[str] = []
    for batch, out, err in results:
        if out is None:
            failed_batches += 1
            if err:
                errors.append(err)
            continue
        try:
            with db_factory() as db:
                added = merge_candidates(db, project_id, out, mode, source_text=batch)
            total_added += len(added)
        except Exception as exc:  # noqa: BLE001 归并失败必须显式上报
            failed_batches += 1
            errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
    pruned = 0
    try:
        with db_factory() as db:
            pruned = prune_ambiguous_aliases(db, project_id)
    except Exception:  # noqa: BLE001 清理失败不反向破坏已归并知识
        pass
    return {
        "batches": len(batches),
        "added": total_added,
        "failed_batches": failed_batches,
        "pruned_aliases": pruned,
        "errors": errors[:5],
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }
