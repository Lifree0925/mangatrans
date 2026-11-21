"""章节剧情卡：重要情节、伏笔与前情依赖（窗口拼接后连续一致的关键）。"""

import json

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.llm.json_utils import parse_llm_json
from app.llm.protocol import LLMProvider
from app.models.document import Chapter, SourceSegment
from app.models.storyline import ChapterStoryline

PROMPT = """你是剧本剧情分析专家。阅读下面这一章的原文，生成剧情卡，用于英文翻译时保持前后连续一致。

1. summary：本章内容摘要（2-3 句）。
2. key_events：本章重要情节（按发生顺序，5-10 条，每条一句话，含关键人物与因果）。
3. foreshadowing：本章埋下的伏笔/悬念（后文才揭晓的事实；翻译本章时不得提前剧透）。
4. prior_deps：本章依赖的前文事实（前情，翻译本章必须与之一致）。
5. character_states：本章结束时主要人物的状态/立场变化（人物名：状态）。

只依据原文；不确定的留空。只返回 JSON。
本章原文：
{text}"""


class StorylineModel(BaseModel):
    summary: str = ""
    key_events: list[str] = Field(default_factory=list)
    foreshadowing: list[str] = Field(default_factory=list)
    prior_deps: list[str] = Field(default_factory=list)
    character_states: dict[str, str] = Field(default_factory=dict)


async def generate_storyline(
    provider: LLMProvider, model: str, text: str
) -> StorylineModel:
    resp = await provider.chat(
        model=model,
        messages=[{"role": "user", "content": PROMPT.format(text=text)}],
        response_schema=StorylineModel.model_json_schema(),
        temperature=0.2,
    )
    data = parse_llm_json(resp.content)
    return StorylineModel.model_validate(data)


async def generate_for_document(
    db_factory, project_id: str, source_document_id: str, provider: LLMProvider, model: str
) -> dict:
    """逐章生成剧情卡；已有卡片则新增版本（可追溯）。"""
    with db_factory() as db:
        chapters = list(
            db.scalars(
                select(Chapter)
                .where(Chapter.source_document_id == source_document_id)
                .order_by(Chapter.chapter_no)
            )
        )
        segs = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.source_document_id == source_document_id)
                .order_by(SourceSegment.segment_no)
            )
        )
    seg_by_ch = {}
    for s in segs:
        if s.chapter_id:
            seg_by_ch.setdefault(s.chapter_id, []).append(s)

    import asyncio

    async def one_chapter(ch: Chapter) -> tuple[bool, bool]:
        text = "".join(s.source_text for s in seg_by_ch.get(ch.id, []))
        if len(text.strip()) < 20:
            return False, False
        try:
            card = await generate_storyline(provider, model, text)
        except Exception:  # noqa: BLE001 单章失败不阻塞
            return False, True
        with db_factory() as db:
            base = (
                db.scalar(
                    select(ChapterStoryline.version).where(
                        ChapterStoryline.chapter_id == ch.id
                    )
                    .order_by(ChapterStoryline.version.desc()).limit(1)
                )
                or 0
            )
            db.add(
                ChapterStoryline(
                    project_id=project_id,
                    chapter_id=ch.id,
                    version=base + 1,
                    card_json=json.dumps(card.model_dump(), ensure_ascii=False),
                    status="generated",
                )
            )
            db.commit()
        return True, False

    sem = asyncio.Semaphore(3)  # 章节并发

    async def limited(ch: Chapter):
        async with sem:
            return await one_chapter(ch)

    results = await asyncio.gather(*(limited(ch) for ch in chapters))
    generated = sum(1 for ok, failed_ in results if ok)
    failed = sum(1 for _, failed_ in results if failed_)
    return {"generated": generated, "failed": failed}
