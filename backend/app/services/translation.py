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
- TERM & PLACE CONSISTENCY: a place, character title or proper noun must use the SAME standard
  translation given in the knowledge/term list throughout the script (exact casing included,
  e.g. always "Wolf King", never "wolf king" or "werewolf king"). Different Chinese names for the
  same place must map to the SAME English name.
- LOCALIZE MARKS: 画外音 / （OS）/ O.S. / (OS) must be rendered as "(voice-over)"; 内景/外景 as INT./EXT.
- PERSON & NUMBER: keep the source's person and number. Third-person references like
  「姐姐带你走」 must stay third person (e.g. "Big sister will take you"), never switch to "I".
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
- TERM & PLACE CONSISTENCY: use the standard translation from the term list with exact casing
  (e.g. "Wolf King", "Anti-magic Handcuffs"); the same place must keep the same English name
  across the whole script — never introduce a second rendering.
- PERSON & NUMBER: keep the source's person and number (「姐姐」 stays third person, never "I").
- LOCALIZE MARKS: 画外音/（OS）/O.S. → "(voice-over)".
- Choose precise, natural English idioms for physical actions; avoid awkward literal renderings.
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


class RepairOutput(BaseModel):
    """QA 定点修复输出：只包含问题段的译文。"""
    window_id: str
    translations: list[TranslationEntry]


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
        # 输出预算耗尽被截断：思考模式最易挤占输出预算 → 关思考重试一次
        if thinking == "enabled":
            resp = await provider.chat(
                model=model,
                messages=messages,
                response_schema=schema,
                temperature=0.3,
                thinking="disabled",
                reasoning_effort=None,
            )
            if resp.finish_reason != "length":
                try:
                    return parse_llm_json(resp.content)
                except ValueError:
                    pass
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
            if thinking == "enabled":
                # 同上：关思考再试一次，避免润色窗口因思考占预算反复截断
                resp = await provider.chat(
                    model=model,
                    messages=messages,
                    response_schema=schema,
                    temperature=0.2,
                    thinking="disabled",
                    reasoning_effort=None,
                )
                if resp.finish_reason != "length":
                    try:
                        return parse_llm_json(resp.content)
                    except ValueError:
                        pass
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
    # 窗口内重复译文校验：相邻段译文完全相同是「台词重复+丢失」的根因
    for sid in sorted(duplicate_segment_ids(translations)):
        errors.append(f"重复译文: {sid}")
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


def duplicate_segment_ids(translations: list[dict]) -> set[str]:
    """返回译文内容重复（长度>40）的段 id 集合。"""
    seen: dict[str, str] = {}
    dup: set[str] = set()
    for t in translations:
        text = (t.get("target_text") or "").strip()
        if len(text) > 40 and text in seen:
            dup.add(t.get("segment_id") or "")
            dup.add(seen[text])
        else:
            seen[text] = t.get("segment_id") or ""
    dup.discard("")
    return dup


def locate_problem_segments(segments: list[dict], translations: list[dict]) -> set[str]:
    """整窗校验失败后定位问题段：缺失 / 空译文 / 段内缺行 / 重复译文。"""
    bad: set[str] = set()
    got = {t.get("segment_id") for t in translations}
    for s in segments:
        sid = s["segment_id"]
        if sid not in got:
            bad.add(sid)
            continue
        one = [t for t in translations if t.get("segment_id") == sid]
        if validate_window([{"segment_id": sid, "source_text": s["source_text"]}], one):
            bad.add(sid)
    bad |= duplicate_segment_ids(translations)
    return bad


async def repair_segments(
    *,
    provider: LLMProvider,
    model: str,
    window_id: str,
    context: ContextResult,
    draft: list[dict],
    issues: list[dict],
    seg_ids: list[str],
) -> dict[str, str]:
    """QA 定点修复：只重译问题段，窗口其余译文保持不变（避免整窗重做浪费时间）。

    复用原窗口上下文与 Draft 保持连续性，输出仅覆盖 seg_ids 中的段。
    """
    target = sorted(set(seg_ids))
    if not target:
        return {}
    issue_block = "\n".join(
        f"- [{i.get('issue_type','')}] segment {i.get('segment_id','?')}: {i.get('explanation','')}"
        + (f"（建议：{i.get('suggested_fix')}）" if i.get("suggested_fix") else "")
        for i in issues[:12]
    )
    sub_draft = [d for d in draft if d.get("segment_id") in target]
    user = (
        context.prompt_text
        + "\n\n[FAITHFUL DRAFT — ONLY THE PROBLEM SEGMENTS]\n"
        + json.dumps(sub_draft, ensure_ascii=False, indent=2)
        + "\n\n[QA ISSUES]\n"
        + issue_block
        + "\n\nRepair ONLY these segment_ids: "
        + ", ".join(target)
        + ". Do NOT output any other segment. Keep all other segments unchanged. Return ONLY the required JSON."
    )
    system = POLISH_SYSTEM + (
        "\n\nYou are REPAIRING only the listed segments flagged by QA. "
        "Fix every issue for those segments while keeping their facts identical to the draft."
    )
    out = await _chat_json_validate(
        provider, model, system, user, RepairOutput.model_json_schema(), RepairOutput,
        thinking="enabled", reasoning_effort="low",
    )
    return {
        e.segment_id: e.target_text
        for e in out.translations
        if e.segment_id in target and e.target_text.strip()
    }


def request_hash_of(segments: list[dict], snapshot: dict, model: str, prompt_version: str) -> str:
    payload = {
        "segments": [s["segment_id"] for s in segments],
        "snapshot": snapshot,
        "model": model,
        "prompt_version": prompt_version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
