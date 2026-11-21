"""两段式翻译：Draft 锁事实 → Polish 情境自然化。"""

import hashlib
import json

from app.llm.json_utils import parse_llm_json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.llm.protocol import LLMError, LLMProvider
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
- STRICT LINE COVERAGE: every non-empty line of each source segment (stage directions,
  sound-effect lines, scene labels, action lines) MUST appear in your translation —
  one line for one line. Never merge or omit a line; omitting one line shifts all
  following segments and corrupts the whole script.
- End with a "carry_over" summary of the story state at the END of this window:
  forms_of_address (how characters currently address each other), ongoing_state (one
  sentence describing the current situation), open_threads (unresolved threads started
  in or before this window), active_expressions (recurring phrasings/mannerisms in use).
  This state is handed to the next window so the whole script stays continuous.
- Return ONLY the required JSON. No commentary."""

POLISH_SYSTEM = """You are a professional Chinese-to-English screenplay translator.
This is the POLISH pass. Rewrite the provided faithful draft into natural, idiomatic English suitable for screen dialogue.
Hard constraints:
- You MUST NOT change any fact, information point, emotional intensity, or speaker intent relative to the draft.
- You may only change wording, sentence structure, register, and naturalness.
- Use the scene context card and carry-over state to make lines fit the situation accurately.
- Use approved character names and terminology as given.
- If the draft flagged ambiguity, keep the ambiguity.
- STRICT LINE COVERAGE: your output must keep EVERY non-empty line of the source segment
  (stage directions, sound-effect lines, scene labels, action lines) — never drop or
  merge a line from the draft.
- Return ONLY the required JSON. No commentary."""


class TranslationEntry(BaseModel):
    segment_id: str
    target_text: str
    notes: list[str] = Field(default_factory=list)
    ambiguity: list[str] = Field(default_factory=list)


class CarryOver(BaseModel):
    forms_of_address: list[str] = Field(default_factory=list)
    ongoing_state: str = ""
    open_threads: list[str] = Field(default_factory=list)
    active_expressions: list[str] = Field(default_factory=list)


class DraftOutput(BaseModel):
    window_id: str
    translations: list[TranslationEntry]
    # Draft 阶段顺带输出窗口末剧情状态（carry-over 前置：Polish 并发的前提）
    carry_over: CarryOver | None = None


class PolishOutput(BaseModel):
    window_id: str
    translations: list[TranslationEntry]
    carry_over: CarryOver = Field(default_factory=CarryOver)


async def _chat_json_validate(
    provider: LLMProvider, model: str, system: str, user: str, schema: dict,
    out_model: type, thinking: str | None = "disabled", reasoning_effort: str | None = None,
):
    """调用 + 解析 + Pydantic 校验；校验失败带修复提示重试一次（思考模式输出易带瑕疵）。"""
    data = await _chat_json(
        provider, model, system, user, schema, thinking=thinking, reasoning_effort=reasoning_effort
    )
    try:
        return out_model.model_validate(data)
    except ValidationError:
        user_fixed = user + (
            "\n\n[FIX] Your last reply had schema errors (missing fields or wrong types). "
            "Return ONLY the complete JSON matching the required schema. No reasoning, no commentary."
        )
        data = await _chat_json(
            provider, model, system, user_fixed, schema,
            thinking=thinking, reasoning_effort=reasoning_effort,
        )
        try:
            return out_model.model_validate(data)
        except ValidationError:
            # 兜底：思考模式输出持续不合格时退到非思考再试一次，保证窗口完成
            if thinking == "enabled":
                data = await _chat_json(
                    provider, model, system, user_fixed, schema, thinking="disabled"
                )
                return out_model.model_validate(data)
            raise


async def _chat_json(
    provider: LLMProvider, model: str, system: str, user: str, schema: dict,
    thinking: str | None = "disabled", reasoning_effort: str | None = None,
):
    """调用并解析 JSON；输出格式瑕疵时重试一次（带修复提示）。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    resp = await provider.chat(
        model=model,
        messages=messages,
        response_schema=schema,
        temperature=0.3,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )
    if resp.finish_reason == "length":
        # 输出预算耗尽被截断：重试大概率同样截断，直接抛出明确错误
        raise LLMError("模型输出被截断（finish_reason=length）", retryable=False, code="output_truncated")
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
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        )
        if resp.finish_reason == "length":
            raise LLMError("模型输出被截断（finish_reason=length）", retryable=False, code="output_truncated")
        return parse_llm_json(resp.content)


async def translate_window_draft(
    *,
    provider: LLMProvider,
    model: str,
    window_id: str,
    context: ContextResult,
) -> list[dict]:
    """Draft 阶段：只锁事实，不依赖 carry-over，可安全并发；顺带输出 carry-over。

    carry-over 由 Draft 生成（而非 Polish）后，Polish 即可窗口级并发：
    各窗口 Polish 消费的是前一窗口 Draft 已锁定的事实状态，不依赖前一窗口 Polish。
    """
    draft = await _chat_json_validate(
        provider, model, DRAFT_SYSTEM, context.prompt_text,
        DraftOutput.model_json_schema(), DraftOutput, thinking="disabled",
    )
    return [t.model_dump() for t in draft.translations], (
        draft.carry_over.model_dump() if draft.carry_over else None
    )


async def translate_window_polish(
    *,
    provider: LLMProvider,
    model: str,
    window_id: str,
    context: ContextResult,
    draft: list[dict],
    carry_over: dict | None,
    segments: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """Polish 阶段：情境自然化 + carry-over 生成；单窗口内串行。"""
    draft_block = "\n\n[FAITHFUL DRAFT]\n" + json.dumps(draft, ensure_ascii=False, indent=2)
    polish = await _chat_json_validate(
        provider, model, POLISH_SYSTEM, context.prompt_text + draft_block,
        PolishOutput.model_json_schema(), PolishOutput, thinking="enabled", reasoning_effort="low",
    )
    polish_list = [t.model_dump() for t in polish.translations]
    # 完整性复查：译文显著短于原文的段判定为漏译（截断），单段重译一次
    seg_map = {s["segment_id"]: s for s in (segments or [])}
    ids = {x["segment_id"] for x in draft}
    src_by_id = {sid: seg_map[sid] for sid in ids if sid in seg_map}
    incomplete = [
        e
        for e in polish_list
        if e["segment_id"] in src_by_id
        and len(src_by_id[e["segment_id"]].get("source_text", "")) > 200
        and len(e["target_text"]) < len(src_by_id[e["segment_id"]]["source_text"]) * 0.6
    ]
    for e in incomplete:
        seg = src_by_id[e["segment_id"]]
        fixed = await _retranslate_incomplete(provider, model, seg)
        if fixed:
            e["target_text"] = fixed
            e["notes"] = e.get("notes", []) + ["incomplete-prev-replaced-by-single-segment-retranslation"]
    return polish_list, polish.carry_over.model_dump()


async def translate_window(
    *,
    provider: LLMProvider,
    model: str,
    window_id: str,
    context: ContextResult,
    segments: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """兼容入口：串行完成 Draft+Polish（测试与独立调用用）。"""
    draft, _draft_carry = await translate_window_draft(
        provider=provider, model=model, window_id=window_id, context=context
    )
    polish, carry = await translate_window_polish(
        provider=provider, model=model, window_id=window_id, context=context,
        draft=draft, carry_over=None, segments=segments,
    )
    return draft, polish, carry


async def _retranslate_incomplete(provider: LLMProvider, model: str, seg: dict) -> str | None:
    """漏译段单独重译：只译这一段，明确要求译完整个原文。"""
    from app.llm.json_utils import parse_llm_json

    user = (
        "Translate the ENTIRE source segment below into English. "
        "A previous attempt stopped early and missed the second half — you MUST translate every sentence to the end.\n\n"
        f"[{seg['segment_id']}] {seg['source_text']}"
    )
    schema = {
        "type": "object",
        "properties": {"target_text": {"type": "string"}},
        "required": ["target_text"],
    }
    try:
        data = await _chat_json(provider, model, DRAFT_SYSTEM, user, schema)
        text = (data or {}).get("target_text", "").strip()
        if len(text) >= len(seg["source_text"]) * 0.6:
            return text
    except (LLMError, ValueError):
        pass
    return None


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
    polish = await _chat_json_validate(
        provider, model, fix_system, context.prompt_text + draft_block,
        PolishOutput.model_json_schema(), PolishOutput, thinking="enabled", reasoning_effort="low",
    )
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
    # 多行段行数校验：译文缺行会让后续段整体错位（历史漏译/标签错位的根因）
    src_by_id = {s["segment_id"]: s for s in segments}
    for t in translations:
        src = src_by_id.get(t.get("segment_id"))
        if src is None:
            continue
        src_lines = [x for x in src.get("source_text", "").strip().split("\n") if x.strip()]
        tgt_lines = [x for x in t.get("target_text", "").strip().split("\n") if x.strip()]
        if len(src_lines) > 1 and len(tgt_lines) < len(src_lines):
            errors.append(
                f"段内缺行: {t.get('segment_id')} 原文 {len(src_lines)} 行 / 译文 {len(tgt_lines)} 行"
            )
    return errors


def request_hash_of(segments: list[dict], snapshot: dict, model: str, prompt_version: str) -> str:
    payload = {
        "segments": [s["segment_id"] for s in segments],
        "snapshot": snapshot,
        "model": model,
        "prompt_version": prompt_version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
