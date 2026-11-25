"""两段式翻译：Draft 锁事实 → Polish 情境自然化。"""

import hashlib
import json
import re
import threading
from collections import OrderedDict

from app.llm.json_utils import parse_llm_json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.llm.protocol import LLMError, LLMProvider
from app.prompts.script_translation_prompt import (
    append_thinking_standard,
    thinking_standard_block,
)
from app.services.context_builder import ContextResult
from app.services.term_match import expected_speakers, normalize
from app.utils.token_budget import estimate_tokens

PROMPT_VERSION = "v6"

_WINDOW_CONVERSATIONS: OrderedDict[tuple[str, str, str], dict] = OrderedDict()
_WINDOW_CONVERSATIONS_LOCK = threading.Lock()
_WINDOW_CONVERSATIONS_MAX = 2000


def _conversation_key(task_id: str, window_key: str, model: str) -> tuple[str, str, str]:
    return task_id, window_key, model


def _conversation_cache_enabled(provider: LLMProvider, task_id: str | None, window_key: str | None) -> bool:
    caps = getattr(provider, "capabilities", None)
    return bool(
        task_id
        and window_key
        and getattr(caps, "supports_conversation_cache", False)
    )


def _put_window_conversation(
    task_id: str, window_key: str, model: str, system: str, draft_user: str,
    draft_assistant: str,
) -> None:
    key = _conversation_key(task_id, window_key, model)
    with _WINDOW_CONVERSATIONS_LOCK:
        _WINDOW_CONVERSATIONS[key] = {
            "system": system,
            "draft_user": draft_user,
            "draft_assistant": draft_assistant,
        }
        _WINDOW_CONVERSATIONS.move_to_end(key)
        while len(_WINDOW_CONVERSATIONS) > _WINDOW_CONVERSATIONS_MAX:
            _WINDOW_CONVERSATIONS.popitem(last=False)


def _get_window_conversation(
    task_id: str | None, window_key: str | None, model: str,
) -> dict | None:
    if not task_id or not window_key:
        return None
    key = _conversation_key(task_id, window_key, model)
    with _WINDOW_CONVERSATIONS_LOCK:
        value = _WINDOW_CONVERSATIONS.get(key)
        if value is None:
            return None
        _WINDOW_CONVERSATIONS.move_to_end(key)
        return dict(value)

DRAFT_SYSTEM = """You are a professional Chinese-to-English screenplay translator.
This is the FAITHFUL DRAFT pass. Translate ONLY the supplied source segments.
Only the SOURCE SEGMENTS block is source content to translate; all other blocks are reference material.

FACT RULES:
- Lock every fact: characters, speaker, relationships, actions, negation, conditions, causality,
  chronology, quantities, and emotional cues must match the source exactly.
- SCOPE: preserve time, frequency, quantity and quantifier scope exactly. 「五年没去」 means
  "hasn't been there in five years", never "has never been there".
- RELATIONS: preserve the exact kinship/social role expressed in the source or established by
  project context. Never substitute a different relative (mother/aunt, brother/uncle, etc.).
  If the referent is clearly identified and project facts give the specific relationship, use
  that relationship; do not replace it with a generic guess. If identity is unclear, keep the
  source wording and record the ambiguity.
- Never invent, infer, explain, soften, intensify, or add information. Literalness is acceptable
  here only where needed to preserve facts; do not paraphrase away details.
- Preserve ambiguity; record it in the ambiguity field instead of guessing.
- EXISTING ENGLISH TITLE: copy an existing English title verbatim; never rewrite it.
- PERSON & NUMBER: preserve the source's person and number. 「姐姐带你走」 must remain third
  person ("Big sister will take you"), never become "I".
- SELF-REFERENCE: a speaker normally refers to themselves as I/me/my, not by their own name.
  Do not turn "I can't hold them off" into "Leo can't hold them off" merely because the source
  uses the character's name for emphasis or childish speech.
- NAMES & ADDRESS: keep approved forms of address. In contemporary formal English, a courtesy
  title takes a family name, never a given name: "Mr. Smith", not "Mr. Richard"; "Mrs. Parker",
  not "Mrs. Helen". If the family name is unknown, use an approved form rather than inventing one.

STRUCTURAL RULES:
- Translate each segment independently and keep its exact segment_id. NEVER move content from
  one segment_id to another.
- Keep every non-empty source line as one corresponding output line. Never merge/drop lines.
  Preserve speaker tags, scene numbers, △ action markers, 【】 sound-effect lines, and (OS) cues.
- HEADINGS AND METADATA ARE NOT DIALOGUE. Scene headings, location names, character lists,
  scene numbers and production labels must never be quoted, paraphrased, or inserted into a
  spoken line. A heading segment stays a heading; a dialogue segment stays spoken language.
- 画外音 / （OS）/ O.S. / (OS) → "(voice-over)"; 内景/外景 → INT./EXT.

TERMINOLOGY:
- FORCED TERMS are approved proper names/titles/place names: use them exactly, with exact casing.
- CONTEXTUAL TERMS are preferred vocabulary only: translate them naturally for the current line;
  never force a location-like phrase where the Chinese word is a generic common noun.
- The same named place/person must keep one English name across the whole script.
- Recurring plot-critical substances, devices, conditions, ranks and in-world labels must keep
  one stable meaning and name. Do not turn a toxin into a pill, or a rank into a different role.

Finally return carry_over: forms_of_address, ongoing_state, open_threads, active_expressions.
Return ONLY the required JSON. No commentary or explanation."""

POLISH_SYSTEM = """You are a professional Chinese-to-English screenplay translator and dialogue editor.
This is the POLISH pass. Rewrite the faithful draft into natural, idiomatic screen English.
Only the SOURCE SEGMENTS block is source content to translate; all other blocks are reference material.

DRAFT IS A SEMANTIC ANCHOR, NOT A STYLE TEMPLATE:
- The faithful draft is a fact checklist. Its wording, syntax and sentence shape are disposable.
- Do not improve a literal draft by swapping words inside its Chinese-shaped frame. Rebuild the
  line from the source: speaker, addressee, speech act, subtext, then the words an American actor
  would actually say in this scene.
- If the draft preserves Chinese topic-comment order, stacked modifiers, noun-phrase fragments,
  or an explanatory clause that restates what both characters already know, discard that syntax
  and write the line natively. Preserve facts, not sentence shape.
- A grammatically correct calque is still a failed polish. If an American actor would not say it,
  rewrite it even when every source word seems to have an English counterpart.

MEANING FIRST:
- Never change facts, information points, speaker intent, emotional intensity, relationship,
  chronology, or ambiguity. Wording, syntax, register and naturalness are the only allowed changes.
- SCOPE: keep time, frequency, quantity and quantifier scope exact. A denial limited to a period
  stays limited: 「五年没去」 → "I haven't been there in five years", never "I've never been there".
- RELATIONS & ADDRESS: kinship and forms of address must match the source and project facts.
  If the referent is clear and project facts say "mother", do not write "aunt"; project facts
  outrank a generic kinship guess. In contemporary formal English, a courtesy title takes a
  family name, never a given name: "Mr. Smith", never "Mr. Richard"; "Mrs. Parker", never
  "Mrs. Helen". Use an approved nickname as a nickname and a formal name as a formal name.
- Do not add lines, beats, explanations, emotions, backstory, or stage directions.
- EXISTING ENGLISH TITLE: copy it verbatim.
- Keep the exact segment_id and output one line per source line. Never move content between IDs.
- Preserve speaker labels, scene numbers, △ markers, 【】 sound-effect lines and (OS) cues.
- 画外音 / （OS）/ O.S. / (OS) → "(voice-over)".
- PERSON & NUMBER: keep the source's person and number (「姐姐」 stays third person, never "I").
- SELF-REFERENCE: use I/me/my when a character speaks about themselves, including children;
  do not replace it with the character's name. Only keep third-person self-reference when the
  source deliberately makes it a childlike verbal tic, and never make the child sound younger
  or more childish than the source.
- AGENCY & DIRECTION: preserve who does what to whom. Never reverse subject/object, agent/patient,
  active/passive, causation or benefactive roles. 「我教了儿子四年」 stays "I've been teaching my
  son for four years", never "Four years of raising my son taught me..." because the teacher and
  learner were swapped.
- SPEECH ACT: keep the line doing the same thing (threat, plea, refusal, test, comfort, apology),
  even when the wording is completely rebuilt.

DIALOGUE LOCALIZATION (highest priority for spoken lines):
- Reconstruct the speech act first: what the character is doing to whom (threatening, mocking,
  flirting, dismissing, warning, bargaining). Then write the most natural English line that does
  the same thing. Naturalness is not a fact change; replacing a literal Chinese image with an
  equivalent English expression is required, not optional.
- Do not preserve an opaque metaphor, proverb or lyrical image just because the draft did.
  Use an equivalent English idiom or plain spoken line. Example: a threat rendered as
  "disappear from our world" is not natural English; when the force is "get lost", write
  "Leave us alone" / "Get out of our lives". Example: "a canary who has to cling to a man"
  may become "a kept woman who needs a man to support her" or "a pretty little trophy".
- Insults, flirting and comebacks must sound like English banter, not dictionary glosses.
  Keep the same contempt or warmth and intensity. Example: "cheap hormones" can become a sharper
  natural jab like "bargain-bin charm" when that preserves the mockery; do not explain the insult.
- Never make new compound labels or headline nouns in dialogue. "three knight-daddy candidates"
  and "Suburban Abandoned Winery Basement" are not spoken English. Unpack them into a natural
  phrase that keeps the same playful image or place reference, e.g.
  "That winery basement last night?" when the speaker is referring back to a place.
- Never lift a scene heading, location label, character list or production metadata into a spoken
  line. If a character refers to the place, use the way people actually speak about it.
- Translate identity/role labels as natural English roles supported by context, not as stacked
  descriptors. Do not invent a nationality, rank or job. "African Strongman Leader" must become
  a context-appropriate role such as "an African strongman" or "a warlord", not a headline.
- Preserve implications the source makes clear. If a publicist warns that a lawyer taking a case
  will expose them to a lawsuit, state that fear; do not leave a vague "if he takes her case".

PERFORMANCE RHYTHM & SUBTEXT:
- Write spoken lines for an actor, not for a reader. Give each emotional turn a playable beat:
  short sentences, periods, commas or dashes can create pauses even inside one output line.
  Do not pack several subordinate clauses into one breath just to keep one sentence.
  Example: "Touch my son again, Richard, and I'll expose everything you did five years ago.
  Even if it kills me. Try me." is more playable than one long "and even if it costs me my life,
  I will..." sentence.
- When a character comforts someone in a low moment, acknowledge the present fear instead of
  reminding them of their former excellence. "That fearless, confident woman is still here.
  I see her." is warmer and more specific than "You're always confident, always shining.",
  which can sound like pressure to perform.
- Keep already-natural colloquial lines. "I screwed up" can be better than "I failed";
  do not formalize good spoken English while polishing the rest of the line.
- For commands to an armed or agitated person, translate the de-escalating intent, not the literal
  verb. 「把枪稳住」 in a plea means "Easy with the gun", not "Keep the gun steady" (which sounds
  like an instruction to keep aiming).
- In severe injury or near-death moments, prefer short, present-focused reassurance over
  explanations of motive. "Don't be scared... I've got you..." is more human than
  "I came here to protect you".
- When the source says 活下来, use "stay alive" / "survive"; make the emotional landing direct:
  "Thank you for staying alive. For giving me the chance to tell you I love you." Avoid
  "say this to your face" unless the line is actually confrontational.
- If the source stacks two Chinese metaphors, do not preserve both images. Keep the judgment,
  attitude and intent, and merge or drop the imagery. Example: 山珍海味 + 带刺野草 can become
  "You're used to a very different kind of woman. I'm something new. It'll wear off."
  rather than "You're used to gourmet food. You run into a weed full of thorns like me...".

VOICE AND ENGLISH:
- SPEAKER VOICE & LOCAL PERFORMANCE overrides general style. Match the speaker's personality,
  speech habits and the immediate parenthetical/stage direction. Local emotion wins over profile.
- Write natural spoken English: contractions, phrasal verbs, interruptions, false starts, and
  short punchy lines where appropriate. Do not translate Chinese word order or idioms literally.
- No bookish connectives in dialogue (thus/therefore/nevertheless/moreover). Do not over-explain.
  Do not "improve" the plot or add dramatic flourishes that are not in the source.
- Map Chinese profanity by intensity to equivalent English strength; keep each character's register.
- Use precise physical-action verbs and idiomatic English, not dictionary glosses.
- Preserve subtext, emotional beat, threats, insults and power dynamics; never flatten or
  rationalize what a character is really doing with the line.

NATIVE DIALOGUE AUDIT (silent final pass before returning):
- Read every spoken line as an actor would say it: is this the natural English speech order,
  rhythm and register for this person, relationship and immediate situation?
- Check the line once more without looking at the draft. If the draft's sentence frame is still
  visible, rewrite the line from the speech act rather than editing the draft.
- Remove Chinese information order and explanatory clauses that only exist to make the source
  logic explicit. Native dialogue can imply what both speakers already know.
- Prefer concrete verbs, contractions, idiomatic phrasing and short playable beats over
  literal but grammatically correct translationese.
- Check that each line still performs the same speech act (threat, plea, joke, dismissal,
  confession) with the same emotional force. Naturalness never overrides facts or continuity.

CONTINUITY AND TERMINOLOGY:
- Use approved character names and name forms. A nickname stays a nickname; a formal name stays
  formal. Never replace every alias with the canonical name.
- Use CHAPTER STORYLINE and CARRY-OVER for continuity; never reveal foreshadowing early.
- FORCED TERMS (proper names/titles/places) must use exact canonical spelling/casing.
- CONTEXTUAL TERMS are only preferred vocabulary; adapt them to context. Never force a generic
  common noun into a specific location name.
- The same named place/person must keep one English rendering across the script.
- Recurring plot-critical substances, devices, conditions, ranks and in-world labels must keep
  one stable English name and meaning. Never substitute a synonym that changes what it is:
  a neurotoxin is not a "Neural Pill", and a title is not a different rank.
- Before finalizing, silently audit each line for translationese: unnatural word order,
  literal idiom, stacked nouns, repeated sentence openers, or wrong register. Revise only the
  wording; never add or remove facts.

Return ONLY the required JSON. No commentary."""


CONVERSATION_SYSTEM = """You are a professional Chinese-to-English screenplay translator working in a staged translation session.
Follow the latest stage instruction in the user messages. The SOURCE SEGMENTS block is the only content to translate.
Earlier assistant output may be the faithful draft for the current Polish stage; use it as the fact source.
Return ONLY the JSON required by the latest stage. No commentary.

""" + thinking_standard_block()

DRAFT_STAGE_INSTRUCTION = "STAGE: DRAFT\n\n" + DRAFT_SYSTEM
POLISH_STAGE_INSTRUCTION = (
    "STAGE: POLISH\n\n"
    + POLISH_SYSTEM
    + "\n\nThe faithful draft JSON is the previous assistant message. "
      "Rewrite that draft into natural screen English. Keep every segment_id and every fact. "
      "Do not translate the project reference or repeat it in the output."
)



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
    max_tokens: int | None = None, stage: str = "translate",
    messages: list[dict] | None = None,
):
    """调用 + 解析 + Pydantic 校验；校验失败带修复提示重试一次（思考模式输出易带瑕疵）。"""
    base_messages = list(messages) if messages is not None else [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    data = await _chat_json(
        provider, model, system, user, schema, thinking=thinking,
        reasoning_effort=reasoning_effort, max_tokens=max_tokens, stage=stage,
        messages=base_messages,
    )
    try:
        return out_model.model_validate(data)
    except ValidationError:
        fix_text = (
            "[FIX] Your last reply had schema errors (missing fields or wrong types). "
            "Return ONLY the complete JSON matching the required schema. No reasoning, no commentary."
        )
        if messages is None:
            retry_user = user + "\n\n" + fix_text
            retry_messages = None
        else:
            retry_user = user
            retry_messages = [*base_messages, {"role": "user", "content": fix_text}]
        data = await _chat_json(
            provider, model, system, retry_user, schema,
            thinking=thinking, reasoning_effort=reasoning_effort, stage=stage,
            messages=retry_messages,
        )
        try:
            return out_model.model_validate(data)
        except ValidationError:
            # 兜底：思考模式输出持续不合格时退到非思考再试一次，保证窗口完成
            if thinking == "enabled":
                data = await _chat_json(
                    provider, model, system, retry_user, schema, thinking="disabled", stage=stage,
                    messages=retry_messages,
                )
                return out_model.model_validate(data)
            raise


def _usage_note(resp) -> str:
    u = getattr(resp, "usage", None) or {}
    parts = [f"{k}={v}" for k, v in u.items() if isinstance(v, (int, float))]
    return "（" + ", ".join(parts) + "）" if parts else ""


async def _chat_json(
    provider: LLMProvider, model: str, system: str, user: str, schema: dict,
    thinking: str | None = "disabled", reasoning_effort: str | None = None,
    max_tokens: int | None = None, stage: str = "translate",
    messages: list[dict] | None = None,
):
    """调用并解析 JSON；输出格式瑕疵时重试一次（带修复提示）。

    截断（finish_reason=length）恢复：思考模式下思维链与译文共享 completion 预算，
    关思考后把预算提升给译文（历史做法把预算砍到 4096 导致反复截断）。
    """
    if messages is not None:
        messages = [dict(m) for m in messages]
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = append_thinking_standard(
                str(messages[0].get("content") or ""), thinking
            )
    else:
        system = append_thinking_standard(system, thinking)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    resp = await provider.chat(
        model=model,
        messages=messages,
        response_schema=schema,
        temperature=0.3,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        stage=stage,
    )
    if resp.finish_reason == "length":
        if thinking == "enabled" or thinking is None:
            resp = await provider.chat(
                model=model,
                messages=messages,
                response_schema=schema,
                temperature=0.3,
                thinking="disabled",
                reasoning_effort=None,
                max_tokens=max(max_tokens or 0, 16384),
                stage=stage,
            )
            if resp.finish_reason != "length":
                try:
                    return parse_llm_json(resp.content)
                except ValueError:
                    pass
        raise LLMError(
            f"模型输出被截断（finish_reason=length）{_usage_note(resp)}",
            retryable=False, code="output_truncated",
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
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            stage=stage,
        )
        if resp.finish_reason == "length":
            if thinking == "enabled" or thinking is None:
                # 同上：关思考 + 预算提升给译文，避免反复截断
                resp = await provider.chat(
                    model=model,
                    messages=messages,
                    response_schema=schema,
                    temperature=0.2,
                    thinking="disabled",
                    reasoning_effort=None,
                    max_tokens=max(max_tokens or 0, 16384),
                    stage=stage,
                )
                if resp.finish_reason != "length":
                    try:
                        return parse_llm_json(resp.content)
                    except ValueError:
                        pass
            raise LLMError(
                f"模型输出被截断（finish_reason=length）{_usage_note(resp)}",
                retryable=False, code="output_truncated",
            )
        return parse_llm_json(resp.content)


def estimate_window_output_tokens(source_chars: int) -> int:
    """按窗口源文本字符数估算译文输出 token 数（中文→英文约 1.6x + JSON/carry 冗余 1200）。"""
    return max(2048, min(int(source_chars * 1.6) + 1200, 60000))


def _should_think_for_window(source_chars: int) -> bool:
    """润色思考模式仅用于输出预算可控的窗口：估算输出过大时关思考，先保证不截断。

    思考模式下思维链与译文共享 completion 预算；源文本 ≤ 3000 字符（估算输出 ≤ 6000 token）
    的窗口开思考，超长窗口关思考翻译（译文预算 8K 兜底）。
    """
    return source_chars <= 3000


async def translate_window_draft(
    *,
    provider: LLMProvider,
    model: str,
    window_id: str,
    context: ContextResult,
    source_chars: int | None = None,
    segments: list[dict] | None = None,
    task_id: str | None = None,
    window_key: str | None = None,
) -> list[dict]:
    """Draft 阶段：只锁事实，不依赖 carry-over，可安全并发；顺带输出 carry-over。

    carry-over 由 Draft 生成（而非 Polish）后，Polish 即可窗口级并发：
    各窗口 Polish 消费的是前一窗口 Draft 已锁定的事实状态，不依赖前一窗口 Polish。
    """
    use_conversation = _conversation_cache_enabled(provider, task_id, window_key)
    system = CONVERSATION_SYSTEM if use_conversation else DRAFT_SYSTEM
    user = (
        DRAFT_STAGE_INSTRUCTION + "\n\n" + context.prompt_text
        if use_conversation else context.prompt_text
    )
    messages = (
        [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if use_conversation else None
    )
    draft = await _chat_json_validate(
        provider, model, system, user,
        DraftOutput.model_json_schema(), DraftOutput, thinking="disabled",
        max_tokens=min(8192, estimate_window_output_tokens(source_chars or 2048)),
        stage="draft",
        messages=messages,
    )
    draft_entries = [t.model_dump() for t in draft.translations]
    if segments:
        draft_entries = normalize_translation_entries(segments, draft_entries)
    draft_carry = draft.carry_over.model_dump() if draft.carry_over else None
    if use_conversation and task_id and window_key:
        assistant_payload = {
            "window_id": draft.window_id or window_id,
            "translations": [t.model_dump() for t in draft.translations],
            "carry_over": draft_carry,
        }
        _put_window_conversation(
            task_id, window_key, model, system, user,
            json.dumps(assistant_payload, ensure_ascii=False, separators=(",", ":")),
        )
    return draft_entries, draft_carry


def draft_prompt_block(draft: list[dict]) -> str:
    """Polish 只消费 Draft 的事实文本；去掉 notes/ambiguity 等无关字段压缩上下文。"""
    compact = [
        {
            "segment_id": entry.get("segment_id", ""),
            "target_text": entry.get("target_text", ""),
        }
        for entry in draft
        if entry.get("segment_id")
    ]
    return "\n\n[FAITHFUL DRAFT]\n" + json.dumps(
        compact, ensure_ascii=False, separators=(",", ":")
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
    force_no_thinking: bool = False,
    task_id: str | None = None,
    window_key: str | None = None,
) -> tuple[list[dict], dict]:
    """Polish 阶段：情境自然化 + carry-over 生成；单窗口内串行。"""
    draft_block = draft_prompt_block(draft)
    # 思考模式：思维链与译文共享 completion 预算，max_tokens 给足（32K，provider 层默认）；
    # 超大窗口关思考翻译，译文预算 8K 兜底，先保证不截断（历史 finish_reason=length 根因）
    src_chars = sum(len(s.get("source_text", "")) for s in (segments or []))
    use_thinking = _should_think_for_window(src_chars) and not force_no_thinking
    conversation = (
        _get_window_conversation(task_id, window_key, model)
        if _conversation_cache_enabled(provider, task_id, window_key)
        else None
    )
    if conversation:
        system = conversation["system"]
        user = POLISH_STAGE_INSTRUCTION
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": conversation["draft_user"]},
            {"role": "assistant", "content": conversation["draft_assistant"]},
            {"role": "user", "content": user},
        ]
    else:
        system = POLISH_SYSTEM
        user = context.prompt_text + draft_block
        messages = None
    polish = await _chat_json_validate(
        provider, model, system, user,
        PolishOutput.model_json_schema(), PolishOutput,
        thinking="enabled" if use_thinking else "disabled",
        reasoning_effort="low" if use_thinking else None,
        max_tokens=(
            None if use_thinking else min(8192, estimate_window_output_tokens(src_chars))
        ),
        stage="polish",
        messages=messages,
    )
    polish_list = [t.model_dump() for t in polish.translations]
    if segments:
        polish_list = normalize_translation_entries(segments, polish_list)
    seg_by_id = {s["segment_id"]: s for s in (segments or [])}
    for entry in polish_list:
        text = localize_screenplay_marks(entry.get("target_text") or "")
        seg = seg_by_id.get(entry.get("segment_id", ""))
        if seg and seg.get("segment_type") == "heading":
            src_text = (seg.get("source_text") or "").rstrip()
            # 章节/场景标题不是句子：模型常自作主张加句号，统一去掉。
            if src_text and src_text[-1] not in "。！？!?." and text.endswith("."):
                text = text[:-1].rstrip()
        entry["target_text"] = text
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
        provider=provider, model=model, window_id=window_id, context=context,
        source_chars=sum(len(s.get("source_text", "")) for s in (segments or [])),
        segments=segments,
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
        stage="repair",
    )
    return [t.model_dump() for t in polish.translations], polish.carry_over.model_dump()


def localize_screenplay_marks(text: str) -> str:
    """确定性本地化画外音标记，避免同一稿里 OS / voice-over 混用。"""
    out = text or ""
    out = re.sub(r"[（(]\s*O\.?\s*S\.?\s*[）)]", "(voice-over)", out, flags=re.IGNORECASE)
    out = out.replace("（画外音）", "(voice-over)").replace("(画外音)", "(voice-over)")
    return out


_SCENE_NO_RE = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)")
_DIALOGUE_SRC_RE = re.compile(r"^\s*([^（(：:]+?)\s*(?:[（(][^）)]*[）)])?\s*[:：]\s*(.+)$")
# 英文角色标签：允许多个表演提示（如 (voice-over) (anxious)）、Markdown 加粗，
# 以及冒号/破折号分隔。合法标签不能因为格式差异被误报为“缺少角色标签”。
DIALOGUE_TARGET_RE = re.compile(
    r"^\s*(?:\*\*|__)?\s*([A-Za-z][A-Za-z0-9 .'’&/+_#-]*?)\s*"
    r"(?:\s*\([^)]*\))*\s*(?:\*\*|__)?\s*(?::|：|—|–|\s-\s)\s*(.+)$"
)


_FRONT_MATTER_MARKERS = ("故事大纲", "人物小传", "Story Outline", "Character Bios")


def _lines(text: str) -> list[str]:
    return [x.strip() for x in (text or "").strip().splitlines() if x.strip()]


def _is_front_matter_text(text: str) -> bool:
    """仅豁免剧本前置元数据（片名/大纲/人物小传）的行数硬校验。

    正文动作与旁白仍逐行校验；前置元数据不是表演内容，模型常把段落合并，
    不应因此让整窗反复重试。
    """
    lines = _lines(text)
    return len(lines) > 8 and any(marker in text for marker in _FRONT_MATTER_MARKERS)


def _restore_scene_number(segment: dict, target_text: str) -> str:
    """模型把「1-2 场景标题」写成 INT. 时，确定性地补回场次号。"""
    if segment.get("segment_type") != "heading":
        return target_text
    src_lines = _lines(segment.get("source_text") or "")
    if not src_lines:
        return target_text
    match = _SCENE_NO_RE.match(src_lines[0])
    if not match:
        return target_text
    wanted = f"{match.group(1)}-{match.group(2)}"
    if re.match(r"^\s*\d+\s*[-–]\s*\d+", target_text or ""):
        return target_text
    return f"{wanted} {target_text.lstrip()}" if (target_text or "").strip() else target_text


def _split_action_target(src_text: str, target_text: str) -> str:
    """把模型合并的多行动作/音效译文按原文行数拆回，保留全部文字。

    动作行最终用中文原文交付，但内部译文仍需满足逐行结构；这里先做确定性
    拆分，避免因模型合并行触发整窗失败。无法可靠拆分时保持原样，交给修复层。
    """
    src_lines = _lines(src_text)
    tgt_lines = _lines(target_text)
    if len(src_lines) < 2 or len(tgt_lines) >= len(src_lines):
        return target_text
    if not all(line.startswith(("△", "【")) for line in src_lines):
        return target_text
    body = " ".join(
        re.sub(r"^\s*[△\[]\s*", "", line).strip(" []") for line in tgt_lines
    ).strip()
    if not body:
        return target_text
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", body) if p.strip()]
    if len(parts) < len(src_lines):
        return target_text
    if len(parts) > len(src_lines):
        # 多出的句子并入最后一行，保证行数与原段一致。
        parts = parts[: len(src_lines) - 1] + [" ".join(parts[len(src_lines) - 1 :])]
    rendered: list[str] = []
    for source_line, part in zip(src_lines, parts):
        if source_line.startswith("△"):
            rendered.append(f"△ {part}")
        else:
            rendered.append(f"[{part.strip(' []')}]")
    return "\n".join(rendered)


def normalize_translation_entries(
    segments: list[dict], entries: list[dict],
) -> list[dict]:
    """在进入结构校验前修正模型输出的可恢复偏差。

    1. 丢弃不存在的 segment_id / 重复项；
    2. 当条目数一致但 ID 集合错位时，按源窗口顺序重映射（模型最常见错误）；
    3. 补回场景号；
    4. 把多行动作段的合并译文按句拆回原行。
    """
    expected = [str(s.get("segment_id") or "") for s in segments]
    expected = [sid for sid in expected if sid]
    if not expected or not entries:
        return entries
    expected_set = set(expected)
    by_id: dict[str, dict] = {}
    unknown = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("segment_id") or "")
        if sid not in expected_set:
            unknown += 1
            continue
        if sid not in by_id:
            by_id[sid] = dict(entry)

    canonical: dict[str, dict] = {}
    if len(entries) == len(expected) and (len(by_id) != len(expected) or unknown):
        # 模型通常保持顺序但会臆造一个 ID；在条数完全一致时按顺序重映射。
        # 后续 validate_window 仍会复核每个段的说话人/场次/行结构。
        for seg, entry in zip(segments, entries):
            if isinstance(entry, dict):
                canonical[str(seg["segment_id"])] = {
                    **entry, "segment_id": str(seg["segment_id"])
                }
    else:
        canonical = by_id

    out: list[dict] = []
    for seg in segments:
        sid = str(seg.get("segment_id") or "")
        entry = canonical.get(sid)
        if entry is None:
            continue
        text = entry.get("target_text") or ""
        if not isinstance(text, str):
            text = str(text)
        text = _restore_scene_number(seg, text)
        text = _split_action_target(seg.get("source_text") or "", text)
        out.append({**entry, "segment_id": sid, "target_text": text})
    return out


def _has_wrong_known_speaker(
    target_role: str, expected: set[str], speaker_map: dict[str, str] | None,
) -> bool:
    """只在译文明确用了另一个已知角色名时判错，避免把意译称呼误判为错位。"""
    if not expected or not speaker_map:
        return False
    role = normalize(target_role)
    expected_norm = {normalize(name) for name in expected if name}
    known_norm = {normalize(name) for name in speaker_map.values() if name}
    matched_known = {name for name in known_norm if name and name in role}
    if not matched_known:
        return False
    return not expected_norm.issubset(matched_known)


def _anchor_errors(src: dict, tgt_lines: list[str], speaker_map: dict[str, str] | None) -> list[str]:
    """校验跨语言仍必须保持的结构锚点：说话人、场次号、舞台标记。"""
    errors: list[str] = []
    sid = src.get("segment_id")
    src_lines = [x.strip() for x in src.get("source_text", "").strip().split("\n") if x.strip()]
    if not src_lines:
        return errors

    segment_type = src.get("segment_type") or ""
    # 场景标题必须保留相同的场次号，避免标题/动作整体挪到相邻段。
    m_scene = _SCENE_NO_RE.match(src_lines[0])
    if segment_type == "heading" and m_scene:
        scene_no = f"{m_scene.group(1)}-{m_scene.group(2)}"
        if not tgt_lines or not _SCENE_NO_RE.match(tgt_lines[0]):
            errors.append(f"场景号丢失: {sid} source={scene_no}")
        elif f"{_SCENE_NO_RE.match(tgt_lines[0]).group(1)}-{_SCENE_NO_RE.match(tgt_lines[0]).group(2)}" != scene_no:
            errors.append(f"场景号错位: {sid} source={scene_no} target={tgt_lines[0][:40]}")

    # 对白：每条原文对白行都必须保留角色标签；知识库中有角色名时校验是否换了人。
    if segment_type == "dialogue":
        src_speaker = str(src.get("speaker_name") or "").strip()
        expected = expected_speakers(src_speaker, speaker_map or {}) if speaker_map else set()
        for idx, sl in enumerate(src_lines):
            sm = _DIALOGUE_SRC_RE.match(sl)
            if not sm:
                continue
            if idx >= len(tgt_lines):
                break
            tm = DIALOGUE_TARGET_RE.match(tgt_lines[idx])
            if not tm:
                errors.append(f"对白缺少角色标签: {sid} 第{idx + 1}行")
                continue
            if expected and _has_wrong_known_speaker(tm.group(1), expected, speaker_map):
                errors.append(
                    f"说话人错位: {sid} 原文={src_speaker or sm.group(1).strip()} "
                    f"预期={sorted(expected)} 译文={tm.group(1).strip()}"
                )

    # 舞台标记：△/括号音效行不能整体丢失，否则会改变剧本动作语义。
    def _count_marker(lines: list[str], marker: str) -> int:
        return sum(1 for line in lines if line.startswith(marker))

    if _count_marker(src_lines, "△") != _count_marker(tgt_lines, "△"):
        errors.append(
            f"动作标记数量不一致: {sid} 原文△{_count_marker(src_lines, '△')} / 译文△{_count_marker(tgt_lines, '△')}"
        )
    src_bracket = sum(1 for line in src_lines if line.startswith("【"))
    tgt_bracket = sum(1 for line in tgt_lines if line.startswith(("【", "[")))
    if src_bracket != tgt_bracket:
        errors.append(f"音效/方括号标记数量不一致: {sid} 原文{src_bracket} / 译文{tgt_bracket}")
    return errors


def validate_window(
    segments: list[dict], translations: list[dict],
    speaker_map: dict[str, str] | None = None,
) -> list[str]:
    """校验 segment 完整性，并校验跨语言锚点（说话人/场次/标记）。

    仅校验结构锚点，不做语义改写判断；语义问题交给 QA 定点检查。
    """
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
    # 输出数组顺序不参与结构校验：落库严格按 segment_id 映射，
    # 顺序不同不会写错段；旧校验会让「内容全对但顺序不同」的窗口永久失败。
    for t in translations:
        if not t.get("target_text", "").strip():
            errors.append(f"空译文: {t.get('segment_id')}")
    # 窗口内重复译文校验：只在“原文确实不同且不是人物表/标题”时判错，
    # 避免合法的重复台词、相同人物列表触发整窗重试。
    for sid in sorted(duplicate_segment_ids(translations, segments)):
        errors.append(f"重复译文: {sid}")
    # 多行段行数校验：译文缺行会让后续段整体错位（历史漏译/标签错位的根因）
    src_by_id = {s["segment_id"]: s for s in segments}
    for t in translations:
        src = src_by_id.get(t.get("segment_id"))
        if src is None:
            continue
        # 已有英文标题可压缩成一行；标题后续会确定性回写，不做逐行覆盖校验。
        if src.get("line_count_optional") or _is_front_matter_text(src.get("source_text", "")):
            continue
        src_lines = [x for x in src.get("source_text", "").strip().split("\n") if x.strip()]
        tgt_lines = [x for x in t.get("target_text", "").strip().split("\n") if x.strip()]
        if len(src_lines) > 1 and len(tgt_lines) < len(src_lines):
            errors.append(
                f"段内缺行: {t.get('segment_id')} 原文 {len(src_lines)} 行 / 译文 {len(tgt_lines)} 行"
            )
        errors.extend(_anchor_errors(src, tgt_lines, speaker_map))
    return errors


def duplicate_segment_ids(
    translations: list[dict], segments: list[dict] | None = None,
) -> set[str]:
    """返回疑似错位的重复译文段 id。

    只有原文内容不同、且不属于人物表/标题的段，才把相同译文视为串段；
    相同原文的重复台词与重复人物列表是剧本合法现象。
    """
    src_by_id = {s.get("segment_id"): s for s in (segments or [])}
    meta_prefix = re.compile(r"^\s*(?:人物列表|人物|角色|编剧|作者|类型|集数)\s*[：:]")
    seen: dict[str, str] = {}
    dup: set[str] = set()
    for t in translations:
        text = (t.get("target_text") or "").strip()
        sid = t.get("segment_id") or ""
        if len(text) <= 40:
            continue
        if text not in seen:
            seen[text] = sid
            continue
        other_id = seen[text]
        src_a = src_by_id.get(sid) or {}
        src_b = src_by_id.get(other_id) or {}
        text_a = (src_a.get("source_text") or "").strip()
        text_b = (src_b.get("source_text") or "").strip()
        if normalize(text_a) == normalize(text_b):
            continue
        if any(
            str(x.get("segment_type") or "") == "heading"
            or meta_prefix.match(str(x.get("source_text") or ""))
            for x in (src_a, src_b)
        ):
            continue
        dup.add(sid)
        dup.add(other_id)
    dup.discard("")
    return dup


def locate_problem_segments(
    segments: list[dict], translations: list[dict],
    speaker_map: dict[str, str] | None = None,
) -> set[str]:
    """整窗校验失败后定位问题段：缺失 / 空译文 / 段内缺行 / 重复译文 / 锚点错位。"""
    bad: set[str] = set()
    got = {t.get("segment_id") for t in translations}
    for s in segments:
        sid = s["segment_id"]
        if sid not in got:
            bad.add(sid)
            continue
        one = [t for t in translations if t.get("segment_id") == sid]
        if validate_window(
            [{
                "segment_id": sid,
                "segment_type": s.get("segment_type", ""),
                "speaker_name": s.get("speaker_name"),
                "source_text": s["source_text"],
                "line_count_optional": s.get("line_count_optional", False),
            }],
            one,
            speaker_map=speaker_map,
        ):
            bad.add(sid)
    bad |= duplicate_segment_ids(translations, segments)
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
    line_counts: dict[str, int] | None = None,
    thinking: str | None = "enabled",
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
    line_req = ""
    if line_counts:
        rows = [
            f"- {sid}: exactly {n} non-empty line(s)"
            for sid, n in sorted(line_counts.items())
            if sid in target and n > 1
        ]
        if rows:
            line_req = (
                "\n\n[REQUIRED LINE STRUCTURE]\n"
                + "\n".join(rows)
                + "\nReturn the same number of non-empty lines for each listed segment, "
                "in the same order. Never merge two source lines into one target line. "
                "Keep every △ / 【】 / speaker-label anchor on its corresponding line."
            )
    user = (
        context.prompt_text
        + "\n\n[FAITHFUL DRAFT — ONLY THE PROBLEM SEGMENTS]\n"
        + json.dumps(sub_draft, ensure_ascii=False, indent=2)
        + "\n\n[QA ISSUES]\n"
        + issue_block
        + line_req
        + "\n\nRepair ONLY these segment_ids: "
        + ", ".join(target)
        + ". Do NOT output any other segment. Keep all other segments unchanged. Return ONLY the required JSON."
    )
    system = POLISH_SYSTEM + (
        "\n\nYou are REPAIRING only the listed segments flagged by QA. "
        "Fix every issue for those segments while keeping their facts identical to the draft."
    )
    # 修复通常只涉及少量问题段：给够译文与思考预算，但不固定申请 32K，
    # 避免小上下文模型因 prompt + max_tokens 超过上限而永久失败。
    # 修复只是结构/术语兜底，不应为了几行文本固定申请 16K 思维预算。
    # 关闭思考时按实际输入估算，既降低成本，也避免小上下文模型因预算过大而拒绝。
    if thinking == "enabled":
        repair_budget = min(
            16384,
            max(8192, estimate_tokens(json.dumps(sub_draft, ensure_ascii=False)) * 2 + 2000),
        )
    else:
        repair_budget = min(
            8192,
            max(2048, estimate_tokens(json.dumps(sub_draft, ensure_ascii=False)) * 2 + 1000),
        )
    out = await _chat_json_validate(
        provider, model, system, user, RepairOutput.model_json_schema(), RepairOutput,
        thinking=thinking,
        reasoning_effort="low" if thinking == "enabled" else None,
        max_tokens=repair_budget,
        stage="repair",
    )
    return {
        e.segment_id: localize_screenplay_marks(e.target_text)
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
