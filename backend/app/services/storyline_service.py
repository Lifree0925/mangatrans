"""章节剧情卡：重要情节、伏笔与前情依赖（窗口拼接后连续一致的关键）。"""

import json

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.llm.json_utils import parse_llm_json
from app.llm.protocol import LLMProvider
from app.models.document import Chapter, SourceSegment
from app.models.storyline import ChapterStoryline
from app.services import continuity_service

PROMPT = """你是剧本剧情分析专家。阅读下面这一章的原文，生成剧情卡，用于英文翻译时保持前后连续一致。
直接输出 JSON（不要任何解释、不要推理过程）。

1. summary：本章内容摘要（2-3 句）。
2. key_events：本章重要情节（按发生顺序，5-10 条，每条一句话，含关键人物与因果）。
3. foreshadowing：本章埋下的伏笔/悬念（后文才揭晓的事实；翻译本章时不得提前剧透）。
4. prior_deps：本章依赖的前文事实（前情，翻译本章必须与之一致）。
5. character_states：本章结束时主要人物的状态/立场变化（人物名：状态）。
6. character_state_details：本章结束时主要人物的结构化状态。只写本章原文有依据的变化；
   known_secrets 只列“本章明确揭示且该人物已经知道”的秘密，不要把后文才揭晓的信息提前写入。
7. critical_facts：本章确认的跨章节硬事实，至少覆盖出现的人物关系、身份、时间、地点、
   物品归属、秘密、否定/条件。fact_type 使用 relationship/identity/time/location/possession/
   secret/negation_condition/event；每项写 subject、predicate、object_text、fact。
   只写本章原文有明确依据的事实，不推测后文。

只依据原文；不确定的留空。
本章原文：
{text}"""


class CharacterStateDetail(BaseModel):
    name: str = ""
    identity: str = ""
    relationship_to_protagonist: str = ""
    faction: str = ""
    emotion: str = ""
    form_of_address: str = ""
    known_secrets: list[str] = Field(default_factory=list)
    evidence: str = ""


class CriticalFactCandidate(BaseModel):
    fact_type: str = "event"
    subject: str = ""
    predicate: str = ""
    object_text: str = ""
    fact: str = ""
    reveal_chapter_no: int | None = None


class StorylineModel(BaseModel):
    summary: str = ""
    key_events: list[str] = Field(default_factory=list)
    foreshadowing: list[str] = Field(default_factory=list)
    prior_deps: list[str] = Field(default_factory=list)
    character_states: dict[str, str] = Field(default_factory=dict)
    character_state_details: list[CharacterStateDetail] = Field(default_factory=list)
    critical_facts: list[CriticalFactCandidate] = Field(default_factory=list)


async def generate_storyline(
    provider: LLMProvider, model: str, text: str
) -> StorylineModel:
    resp = await provider.chat(
        model=model,
        messages=[{"role": "user", "content": PROMPT.format(text=text)}],
        response_schema=StorylineModel.model_json_schema(),
        temperature=0.2,
        stage="storyline",
    )
    data = parse_llm_json(resp.content)
    return StorylineModel.model_validate(data)


async def generate_for_document(
    db_factory, project_id: str, source_document_id: str, provider: LLMProvider, model: str,
    on_chapter_done=None,
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
        try:
            text = "".join(s.source_text for s in seg_by_ch.get(ch.id, []))
            if len(text.strip()) < 20:
                return False, False
            try:
                card = await generate_storyline(provider, model, text)
            except Exception:  # noqa: BLE001 单章失败不阻塞
                return False, True
            # 第一集/第一章不存在前情：强制清空，避免模型虚构「上一集依赖」
            if ch.chapter_no == 1:
                card.prior_deps = []
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
                storyline = ChapterStoryline(
                    project_id=project_id,
                    chapter_id=ch.id,
                    version=base + 1,
                    card_json=json.dumps(card.model_dump(), ensure_ascii=False),
                    status="generated",
                )
                db.add(storyline)
                db.flush()
                storyline_id = storyline.id
                db.commit()
            # 状态/事实是派生数据：单独事务同步，失败不能拖垮剧情卡和翻译主流程。
            try:
                with db_factory() as cdb:
                    chapter = cdb.get(Chapter, ch.id)
                    row = cdb.get(ChapterStoryline, storyline_id)
                    if chapter is not None and row is not None:
                        continuity_service.sync_storyline(
                            cdb, project_id, chapter, row, card.model_dump()
                        )
                        cdb.commit()
            except Exception as exc:  # noqa: BLE001
                print(f"backend: continuity sync failed for chapter {ch.chapter_no}: {exc}", flush=True)
            return True, False
        finally:
            # 无论成功/失败/章节过短都释放该章节等待，避免窗口级流水线死等。
            if on_chapter_done is not None:
                on_chapter_done(ch.id)

    sem = asyncio.Semaphore(6)  # 章节并发

    async def limited(ch: Chapter):
        async with sem:
            return await one_chapter(ch)

    results = await asyncio.gather(*(limited(ch) for ch in chapters))
    generated = sum(1 for ok, failed_ in results if ok)
    failed = sum(1 for _, failed_ in results if failed_)
    return {"generated": generated, "failed": failed}
