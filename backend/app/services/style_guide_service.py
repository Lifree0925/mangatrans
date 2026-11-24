"""试译与风格基线：自动挑选代表性场景、两段式试译、归纳并冻结 Style Guide。"""

import json

from app.llm.json_utils import parse_llm_json

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.protocol import LLMProvider
from app.models.document import Scene, SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.translation import StyleGuide, Translation
from app.services.context_builder import build_context
from app.services.importer import sha256
from app.services.translation import PROMPT_VERSION, request_hash_of, translate_window

EMOTION_WORDS = ["怒", "吼", "哭", "笑", "惊", "恐", "恨", "爱", "怨", "激动", "颤抖", "愤怒", "泪"]


class VoiceCard(BaseModel):
    name: str = ""
    voice: str = ""


class StyleGuideModel(BaseModel):
    character_voices: list[VoiceCard] = Field(default_factory=list)
    dialogue_register: str = ""
    action_description_style: str = ""
    cultural_strategy: str = ""
    capitalization_rules: str = ""
    examples: list[dict] = Field(default_factory=list)


GUIDE_PROMPT = """你是剧本翻译风格总监。根据以下试译样本（原文 + polished 译文），归纳一份英文风格基线（Style Guide）。

要求：
1. character_voices：为每个出场人物归纳 voice（用词、句式、语域、称呼习惯、情绪强度）。
2. dialogue_register：整体对白口语化程度与语域；对白必须像英语原创剧，避免翻译腔、堆叠名词和场景标题式表达。
3. action_description_style：动作描述风格（简洁/标准/详细）。
4. cultural_strategy：文化表达转换策略；中文意象、俗语、辱骂和威胁要用等价的英语表达，不保留生硬直译。
5. capitalization_rules：专名/头衔大小写规则。
6. examples：挑选 3-5 条最能代表风格的中英对照；只选自然、可复用的表达。
7. 只依据样本，不编造。
8. 只返回 JSON。

试译样本：
{samples}"""


def pick_trial_windows(db: Session, source_document_id: str) -> list[list[dict]]:
    """挑 3-5 个代表性窗口：人物多/情绪冲突/动作密集/普通叙述。"""
    scenes = list(
        db.scalars(
            select(Scene)
            .where(Scene.source_document_id == source_document_id)
            .order_by(Scene.scene_no)
        )
    )
    segs = list(
        db.scalars(
            select(SourceSegment)
            .where(SourceSegment.source_document_id == source_document_id)
            .order_by(SourceSegment.segment_no)
        )
    )

    def seg_dict(s):
        return {
            "segment_id": s.id,
            "segment_no": s.segment_no,
            "segment_type": s.segment_type,
            "speaker_name": s.speaker_name,
            "source_text": s.source_text,
        }

    windows: list[list[dict]] = []
    if not scenes:
        # 无场景：按 800-1500 字符滚动窗口挑 3 个
        buf: list[dict] = []
        size = 0
        picks = 0
        step = max(1, len(segs) // 4)
        for s in segs:
            buf.append(seg_dict(s))
            size += len(s.source_text)
            if size >= 1200 and picks < 3:
                windows.append(buf)
                buf = []
                size = 0
                picks += 1
        if buf and picks < 3:
            windows.append(buf)
        return windows

    # 打分：speaker 多样性 + 对白量 + 情绪词
    scored: list[tuple[float, list[dict]]] = []
    for scene in scenes:
        group = [seg_dict(s) for s in segs if s.scene_id == scene.id]
        if not group:
            continue
        speakers = {s["speaker_name"] for s in group if s["speaker_name"]}
        dialogues = sum(1 for s in group if s["segment_type"] == "dialogue")
        text = "".join(s["source_text"] for s in group)
        emotion = sum(text.count(w) for w in EMOTION_WORDS)
        narrative_len = sum(len(s["source_text"]) for s in group if s["segment_type"] == "narrative")
        score = len(speakers) * 3 + dialogues * 1.5 + emotion * 2
        scored.append((score, group, narrative_len, dialogues))

    if not scored:
        return windows
    by_score = sorted(scored, key=lambda x: x[0], reverse=True)
    windows.append(by_score[0][1])  # 人物多/情绪冲突 top1
    if len(by_score) > 1:
        windows.append(by_score[1][1])
    by_narrative = sorted(scored, key=lambda x: x[2], reverse=True)
    if by_narrative[0][1] not in windows:
        windows.append(by_narrative[0][1])  # 动作/叙述密集
    by_dialogue = sorted(scored, key=lambda x: x[3], reverse=True)
    if by_dialogue[0][1] not in windows:
        windows.append(by_dialogue[0][1])  # 对白密集
    # 普通场景：中间段
    if len(by_score) > 2:
        mid = by_score[len(by_score) // 2][1]
        if mid not in windows:
            windows.append(mid)
    return windows[:5]


async def run_trial(
    db_factory,
    project_id: str,
    source_document_id: str,
    provider: LLMProvider,
    model: str,
) -> dict:
    """试译全流程：挑场景 → 两段式翻译 → 入库（复用正式译文）→ 归纳 Style Guide 冻结。"""
    with db_factory() as db:
        windows = pick_trial_windows(db, source_document_id)
        # 知识（场景卡已从管线剔除）
        items = [
            {
                "id": k.id,
                "item_type": k.item_type,
                "canonical_key": k.canonical_key,
                "data": json.loads(k.data_json or "{}"),
            }
            for k in db.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.project_id == project_id,
                    KnowledgeItem.status.in_(["approved", "auto_approved"]),
                )
            )
        ]

    samples: list[dict] = []
    translated_windows = 0
    carry: dict | None = None

    for wi, window in enumerate(windows):
        context = build_context(
            segments=window,
            scene_card=None,
            knowledge=items,
            style_guide=None,
            carry_over=carry,
        )
        draft, polished, carry = await translate_window(
            provider=provider,
            model=model,
            window_id=f"TRIAL_{wi:02d}",
            context=context,
            segments=window,
        )
        _persist_window(db_factory, window, draft, polished, carry, context, model)
        translated_windows += 1
        for src, pol in zip(window, polished):
            samples.append(
                {"zh": src["source_text"].strip(), "en": pol["target_text"]}
            )

    # 归纳 Style Guide
    resp = await provider.chat(
        model=model,
        messages=[
            {
                "role": "user",
                "content": GUIDE_PROMPT.format(
                    samples=json.dumps(samples, ensure_ascii=False, indent=2)[:8000]
                ),
            }
        ],
        response_schema=StyleGuideModel.model_json_schema(),
        temperature=0.3,
        stage="style_guide",
    )
    guide = StyleGuideModel.model_validate(parse_llm_json(resp.content))
    with db_factory() as db:
        prev = db.scalar(
            select(func.max(StyleGuide.version)).where(StyleGuide.project_id == project_id)
        ) or 0
        sg = StyleGuide(
            project_id=project_id,
            version=prev + 1,
            guide_json=json.dumps(guide.model_dump(), ensure_ascii=False),
            status="frozen",
        )
        db.add(sg)
        db.commit()
        guide_id = sg.id

    return {"windows": translated_windows, "style_guide_id": guide_id}


def _persist_window(db_factory, window, draft, polished, carry, context, model) -> None:
    """试译译文直接入库（复用为正式译文），draft 与 polished 各一个版本。

    走 insert_translation_row：版本号由单条 INSERT...SELECT 原子分配，跨协程/进程安全。
    """
    from app.services.translation_batch import insert_translation_row

    with db_factory() as db:
        for src, dr, po in zip(window, draft, polished):
            for stage, entry in (("draft", dr), ("polished", po)):
                insert_translation_row(
                    db,
                    segment_id=src["segment_id"],
                    stage=stage,
                    target_text=entry["target_text"],
                    notes_json=json.dumps(entry.get("notes", []), ensure_ascii=False),
                    ambiguity_json=json.dumps(entry.get("ambiguity", []), ensure_ascii=False),
                    model_name=model,
                    provider_id="default",
                    prompt_version=PROMPT_VERSION,
                    context_snapshot_json=json.dumps(context.snapshot, ensure_ascii=False),
                    source_hash=sha256(src["source_text"]),
                )
