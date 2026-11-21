"""角色档案提取：性格特征、说话方式等（Polish 润色的重要依据）。"""

import json
from collections import Counter

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.llm.json_utils import parse_llm_json
from app.llm.protocol import LLMProvider
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeItem

PROMPT = """你是剧本角色分析专家。根据提供的角色名单与各角色的台词样本，为每个主要角色建立角色档案，用于英文翻译时保持说话风格一致。

对每个角色输出：
1. personality：性格特征（依据台词，不编造）。
2. speech_style：说话方式（用词、句长、口头禅、语域、情绪强度、是否爱用反问/命令等）。
3. forms_of_address：对他人/被称呼的方式（从原文摘取）。
4. emotional_range：常见情绪区间。
5. notes：翻译时应注意的其它事项（可空）。

只依据提供的原文；不确定的留空。只返回 JSON。
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


def _build_sample_text(characters: list[str], segs: list[SourceSegment], max_dialogue_chars: int = 400) -> str:
    """组装：角色名单 + 各角色台词样本。"""
    parts = ["角色名单：" + "、".join(characters)]
    samples: dict[str, list[str]] = {c: [] for c in characters}
    for seg in segs:
        name = seg.speaker_name or ""
        if name in samples and len(samples[name]) < 3:
            samples[name].append(seg.source_text.strip())
    parts.append("台词样本：")
    for c in characters:
        lines = samples[c]
        if lines:
            parts.append(f"「{c}」: " + " / ".join(lines)[:max_dialogue_chars])
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
    # 按台词出现频次取主要角色
    counts = Counter(s.speaker_name for s in segs if s.speaker_name)
    character_names = [c for c, _ in counts.most_common(top_n)]
    if not character_names:
        return {"profiles": 0, "note": "未识别出角色台词"}

    text = _build_sample_text(character_names, segs)
    resp = await provider.chat(
        model=model,
        messages=[{"role": "user", "content": PROMPT.format(text=text)}],
        response_schema=CharacterProfiles.model_json_schema(),
        temperature=0.2,
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
        for k in knowledge:
            if k.canonical_key not in by_name:
                continue
            prof = by_name[k.canonical_key]
            merged = dict(json.loads(k.data_json or "{}"))
            merged.update(
                {
                    "personality": prof.personality,
                    "speech_style": prof.speech_style,
                    "forms_of_address": prof.forms_of_address,
                    "emotional_range": prof.emotional_range,
                    "notes": prof.notes,
                }
            )
            k.data_json = json.dumps(merged, ensure_ascii=False)
            k.version += 1
            updated += 1
        db.commit()
    return {"profiles": updated, "characters": len(character_names)}
