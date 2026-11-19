"""场景卡生成：每场一次调用，自动生成、默认生效。"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.protocol import LLMProvider
from app.models.document import Scene, SourceSegment
from app.models.knowledge import SceneContextCard
from app.schemas.knowledge import SceneCardModel

CARD_PROMPT = """你是剧本情境分析助手。阅读下面这场戏的原文，生成场景情境卡。

要求：
1. 只依据原文，不编造；不确定的字段留空字符串。
2. spoiler_guard 只放"本场之后才揭晓、本场翻译时不得提前泄露"的事实；没有就空列表。
3. intent_notes 标注关键台词的意图与潜台词（可空）。
4. 只返回 JSON。

场景原文：
{text}"""


async def generate_card(provider: LLMProvider, text: str, model: str) -> SceneCardModel:
    resp = await provider.chat(
        model=model,
        messages=[{"role": "user", "content": CARD_PROMPT.format(text=text)}],
        response_schema=SceneCardModel.model_json_schema(),
        temperature=0.2,
    )
    data = json.loads(resp.content)
    return SceneCardModel.model_validate(data)


async def generate_cards_for_document(
    db_factory, project_id: str, source_document_id: str, provider: LLMProvider, model: str
) -> dict:
    with db_factory() as db:
        scenes = list(
            db.scalars(
                select(Scene)
                .where(Scene.source_document_id == source_document_id)
                .order_by(Scene.chapter_id, Scene.scene_no)
            )
        )
        segs = {
            s.scene_id: []
            for s in db.scalars(
                select(SourceSegment).where(
                    SourceSegment.source_document_id == source_document_id
                )
            )
            if s.scene_id
        }
        for s in db.scalars(
            select(SourceSegment).where(SourceSegment.source_document_id == source_document_id)
        ):
            if s.scene_id in segs:
                segs[s.scene_id].append(s)

    if not scenes:
        # 无场景标题的剧本：按章节合成一个场景卡
        return {"generated": 0, "note": "剧本未识别出场景，跳过场景卡"}

    generated = 0
    for scene in scenes:
        parts = segs.get(scene.id, [])
        if not parts:
            continue
        text = "".join(p.source_text for p in parts)
        if len(text.strip()) < 10:
            continue
        card = await generate_card(provider, text, model)
        with db_factory() as db:
            db.add(
                SceneContextCard(
                    project_id=project_id,
                    source_document_id=source_document_id,
                    scene_id=scene.id,
                    card_json=json.dumps(card.model_dump(), ensure_ascii=False),
                    evidence_json="[]",
                    status="generated",
                )
            )
            db.commit()
        generated += 1
    return {"generated": generated}
