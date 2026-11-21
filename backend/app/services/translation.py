"""两段式翻译：Draft 锁事实 → Polish 情境自然化。"""

import hashlib
import json

from app.llm.json_utils import parse_llm_json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.llm.protocol import LLMProvider
from app.services.context_builder import ContextResult

PROMPT_VERSION = "v1"

DRAFT_SYSTEM = """You are a professional Chinese-to-English screenplay translator.
This is the FAITHFUL DRAFT pass. Translate ONLY the supplied source segments.
Rules:
- Lock all facts: characters, relationships, actions, negation, conditions, causality, chronology, quantities must match the source exactly.
- Word-for-word literalness is ACCEPTABLE at this stage; naturalness is handled in a later pass.
- Do not invent, infer, explain, or add facts.
- If a reference is genuinely ambiguous, preserve the ambiguity and note it in the ambiguity field.
- Preserve all stage directions and emotional details.
- Return ONLY the required JSON. No commentary."""

POLISH_SYSTEM = """You are a professional Chinese-to-English screenplay translator.
This is the POLISH pass. Rewrite the provided faithful draft into natural, idiomatic English suitable for screen dialogue.
Hard constraints:
- You MUST NOT change any fact, information point, emotional intensity, or speaker intent relative to the draft.
- You may only change wording, sentence structure, register, and naturalness.
- Use the scene context card and carry-over state to make lines fit the situation accurately.
- Use approved character names and terminology as given.
- If the draft flagged ambiguity, keep the ambiguity.
- Return ONLY the required JSON. No commentary."""


class TranslationEntry(BaseModel):
    segment_id: str
    target_text: str
    notes: list[str] = Field(default_factory=list)
    ambiguity: list[str] = Field(default_factory=list)


class DraftOutput(BaseModel):
    window_id: str
    translations: list[TranslationEntry]


class CarryOver(BaseModel):
    forms_of_address: list[str] = Field(default_factory=list)
    ongoing_state: str = ""
    open_threads: list[str] = Field(default_factory=list)
    active_expressions: list[str] = Field(default_factory=list)


class PolishOutput(BaseModel):
    window_id: str
    translations: list[TranslationEntry]
    carry_over: CarryOver = Field(default_factory=CarryOver)


async def _chat_json(provider: LLMProvider, model: str, system: str, user: str, schema: dict):
    """调用并解析 JSON；输出格式瑕疵时重试一次（带修复提示）。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    resp = await provider.chat(
        model=model,
        messages=messages,
        response_schema=schema,
        temperature=0.3,
    )
    try:
        return parse_llm_json(resp.content)
    except ValueError:
        messages.append(
            {
                "role": "user",
                "content": "Your previous reply was not valid JSON. Reply again with ONLY the required JSON, no extra text.",
            }
        )
        resp = await provider.chat(
            model=model,
            messages=messages,
            response_schema=schema,
            temperature=0.2,
        )
        return parse_llm_json(resp.content)


async def translate_window(
    *,
    provider: LLMProvider,
    model: str,
    window_id: str,
    context: ContextResult,
) -> tuple[list[dict], dict]:
    """返回 (translations 列表(draft), polished 列表 + carry_over)。"""

    async def _round(system: str, schema, polish: bool = False):
        user = (
            "Faithful draft below. Rewrite it into natural English.\n\n"
            + context.prompt_text
            if polish
            else context.prompt_text
        )
        data = await _chat_json(provider, model, system, user, schema)
        return data

    # Draft 轮
    draft_data = await _round(DRAFT_SYSTEM, DraftOutput.model_json_schema())
    draft = DraftOutput.model_validate(draft_data)

    # Polish 轮：把 draft 结果作为额外输入
    draft_block = "\n\n[FAITHFUL DRAFT]\n" + json.dumps(
        [t.model_dump() for t in draft.translations], ensure_ascii=False, indent=2
    )
    polish_data = await _chat_json(
        provider,
        model,
        POLISH_SYSTEM,
        context.prompt_text + draft_block,
        PolishOutput.model_json_schema(),
    )
    polish = PolishOutput.model_validate(polish_data)
    return [t.model_dump() for t in draft.translations], [
        t.model_dump() for t in polish.translations
    ], polish.carry_over.model_dump()


async def repair_window(
    *,
    provider: LLMProvider,
    model: str,
    window_id: str,
    context: ContextResult,
    draft: list[dict],
    issues: list[dict],
) -> tuple[list[dict], dict]:
    """质检打回重做：把问题清单作为修正指令，重新生成 polished。"""
    issue_block = "\n".join(
        f"- [{i.get('issue_type','')}] {i.get('explanation','')}"
        + (f"（建议：{i.get('suggested_fix')}）" if i.get("suggested_fix") else "")
        for i in issues[:12]
    )
    fix_system = POLISH_SYSTEM + (
        "\n\nThe previous translation was rejected by QA with these issues:\n"
        + issue_block
        + "\nRewrite the translations to fix EVERY issue above. Keep all correct parts. Return ONLY the required JSON."
    )
    draft_block = "\n\n[FAITHFUL DRAFT]\n" + json.dumps(draft, ensure_ascii=False, indent=2)
    data = await _chat_json(
        provider,
        model,
        fix_system,
        context.prompt_text + draft_block,
        PolishOutput.model_json_schema(),
    )
    polish = PolishOutput.model_validate(data)
    return [t.model_dump() for t in polish.translations], polish.carry_over.model_dump()


def validate_window(segments: list[dict], translations: list[dict]) -> list[str]:
    """校验：segment_id 完整、唯一、顺序。返回错误列表。"""
    errors: list[str] = []
    expect = [s["segment_id"] for s in segments]
    got = [t.get("segment_id") for t in translations]
    if sorted(got) != sorted(expect):
        missing = set(expect) - set(got)
        extra = set(got) - set(expect)
        dup = [g for g in got if got.count(g) > 1]
        if missing:
            errors.append(f"缺失 segment: {sorted(missing)[:5]}")
        if extra:
            errors.append(f"多余 segment: {sorted(extra)[:5]}")
        if dup:
            errors.append(f"重复 segment: {sorted(set(dup))[:5]}")
    if not errors and got != expect:
        errors.append("segment 顺序与原文不一致")
    for t in translations:
        if not t.get("target_text", "").strip():
            errors.append(f"空译文: {t.get('segment_id')}")
    return errors


def request_hash_of(segments: list[dict], snapshot: dict, model: str, prompt_version: str) -> str:
    payload = {
        "segments": [s["segment_id"] for s in segments],
        "snapshot": snapshot,
        "model": model,
        "prompt_version": prompt_version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
