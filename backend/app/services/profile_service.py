"""角色档案提取：性格特征、说话方式等（Polish 润色的重要依据）。"""

import json
from collections import Counter

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.llm.json_utils import parse_llm_json
from app.llm.protocol import LLMProvider
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeItem

PROMPT = """你是剧本角色分析专家。根据角色名单和跨全剧抽取的台词样本，为每个主要角色建立英文翻译用的角色声音档案。
直接输出 JSON（不要解释、不要推理过程）。

对每个角色输出：
1. personality：稳定的性格特征，必须有台词/行为依据，不写空泛形容词。
2. speech_style：具体说话方式，包括句长、语域（粗俗/克制/古雅等）、常用句式、反问/命令倾向、口头禅和省略习惯。
3. forms_of_address：角色对他人使用的称呼，以及别人如何称呼该角色（只摘原文）。
4. emotional_range：常见情绪区间，以及情绪爆发时的表达特点。
5. notes：翻译时的具体约束，例如是否用缩略语、是否避免文雅词、是否保留粗口强度、是否常用短句。

硬性要求：
- 只依据提供的原文；不要根据名字猜测性格，不得补写剧情。
- 样本跨全剧分布时，要提炼稳定特征，不要只按第一场戏定型。
- 不要复述台词原文，不要编写台词。
- 同一角色不要拆成多个档案。

角色名单与台词样本：
{text}"""


class CharacterProfile(BaseModel):
    name: str
    personality: str = ""
    speech_style: str = ""
    forms_of_address: list[str] = Field(default_factory=list)
    emotional_range: str = ""
    notes: str = ""


class CharacterProfiles(BaseModel):
    profiles: list[CharacterProfile] = Field(default_factory=list)


def _evenly_spaced(items: list[SourceSegment], limit: int = 6) -> list[SourceSegment]:
    """跨全剧均匀取样，避免只拿开场台词导致角色声音定型错误。"""
    if len(items) <= limit:
        return items
    if limit <= 1:
        return [items[0]]
    indexes = {round(i * (len(items) - 1) / (limit - 1)) for i in range(limit)}
    return [items[i] for i in sorted(indexes)]


def _build_sample_text(
    characters: list[str], segs: list[SourceSegment],
    alias_to_canonical: dict[str, str] | None = None,
    max_dialogue_chars: int = 760,
) -> str:
    """组装：角色名单 + 跨全剧均布台词样本（含局部动作/情绪提示）。"""
    alias_to_canonical = alias_to_canonical or {}
    parts = ["角色名单：" + "、".join(characters)]
    samples: dict[str, list[SourceSegment]] = {c: [] for c in characters}
    for seg in segs:
        raw = (seg.speaker_name or "").strip()
        canonical = alias_to_canonical.get(raw, raw)
        if canonical in samples and seg.segment_type == "dialogue":
            samples[canonical].append(seg)
    parts.append("跨全剧台词样本（按时间顺序均匀抽样）：")
    for c in characters:
        picked = _evenly_spaced(samples[c], 6)
        if picked:
            lines = []
            for seg in picked:
                text = " / ".join(x.strip() for x in seg.source_text.splitlines() if x.strip())
                if text:
                    lines.append(text)
            joined = " || ".join(lines)
            parts.append(f"「{c}」: {joined[:max_dialogue_chars]}")
        else:
            parts.append(f"「{c}」: （无直接台词样本）")
    return "\n".join(parts)


async def extract_profiles(
    db_factory, project_id: str, source_document_id: str, provider: LLMProvider, model: str, top_n: int = 20
) -> dict:
    """提取主要角色档案，写入知识条目的 data_json（personality/speech_style 等）。"""
    with db_factory() as db:
        segs = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.source_document_id == source_document_id)
                .order_by(SourceSegment.segment_no)
            )
        )
        knowledge = list(
            db.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.project_id == project_id, KnowledgeItem.item_type == "character"
                )
            )
        )
        from app.models.knowledge import KnowledgeAlias

        alias_rows = list(
            db.scalars(
                select(KnowledgeAlias).where(
                    KnowledgeAlias.knowledge_id.in_([k.id for k in knowledge])
                )
            )
        ) if knowledge else []
    alias_to_canonical: dict[str, str] = {}
    for k in knowledge:
        alias_to_canonical[k.canonical_key] = k.canonical_key
    for a in alias_rows:
        owner = next((k for k in knowledge if k.id == a.knowledge_id), None)
        if owner is not None:
            alias_to_canonical[a.alias] = owner.canonical_key

    # 按台词出现频次取主要角色（先归并别名）
    counts = Counter(
        alias_to_canonical.get((s.speaker_name or "").strip(), (s.speaker_name or "").strip())
        for s in segs if s.speaker_name
    )
    known_names = set(alias_to_canonical.values())
    character_names = [
        c for c, _ in counts.most_common()
        if c in known_names and c not in {"未知", "旁白"}
    ][:top_n]
    if not character_names:
        return {"profiles": 0, "note": "未识别出知识库中的角色台词"}

    text = _build_sample_text(character_names, segs, alias_to_canonical)
    resp = await provider.chat(
        model=model,
        messages=[{"role": "user", "content": PROMPT.format(text=text)}],
        response_schema=CharacterProfiles.model_json_schema(),
        temperature=0.2,
        stage="profile",
    )
    data = parse_llm_json(resp.content)
    try:
        out = CharacterProfiles.model_validate(data)
    except Exception:  # noqa: BLE001 格式瑕疵降级为空
        return {"profiles": 0, "error": "档案格式解析失败"}

    # 归并到知识条目 data_json
    by_name = {p.name.strip(): p for p in out.profiles if p.name.strip()}
    updated = 0
    with db_factory() as db:
        # 注意：knowledge 来自已关闭的 session（detached），必须按 id 重新取才可写库
        for k in knowledge:
            if k.canonical_key not in by_name:
                continue
            live = db.get(KnowledgeItem, k.id)
            if live is None:
                continue
            prof = by_name[k.canonical_key]
            merged = dict(json.loads(live.data_json or "{}"))
            merged.update(
                {
                    "personality": prof.personality,
                    "speech_style": prof.speech_style,
                    "forms_of_address": prof.forms_of_address,
                    "emotional_range": prof.emotional_range,
                    "notes": prof.notes,
                }
            )
            live.data_json = json.dumps(merged, ensure_ascii=False)
            live.version += 1
            updated += 1
        db.commit()
    return {"profiles": updated, "characters": len(character_names)}
