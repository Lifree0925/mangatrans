"""Context Builder：按窗口相关性组装上下文，生成 Report 与快照。

原则：不把整本知识库塞进 Prompt。角色、术语、规则/关系、章节剧情都按当前
窗口原句、说话人、别名和称呼做相关性筛选；Polish 只收到本窗口需要的内容。
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

from app.services.term_match import (
    build_speaker_map,
    build_term_records,
    normalize,
    select_source_matches,
)
from app.utils.token_budget import estimate_tokens

DEFAULT_BUDGET = 6000
_PROFILE_FIELDS = (
    "translation",
    "personality",
    "speech_style",
    "forms_of_address",
    "role",
    "relationships",
    "emotional_range",
    "notes",
)


@dataclass
class ContextResult:
    prompt_text: str
    report: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    quality: str = "complete"  # complete / partial / needs_review
    total_tokens: int = 0


def _json_block(title: str, payload: dict | str | list) -> str:
    body = json.dumps(payload, ensure_ascii=False, indent=2) if isinstance(payload, (dict, list)) else str(payload)
    return f"\n\n[{title}]\n{body}\n"


def _item_names(item: dict) -> list[str]:
    data = item.get("data", {}) if isinstance(item.get("data"), dict) else {}
    names = [item.get("canonical_key", "")]
    names.extend(item.get("aliases", []) or [])
    names.extend(data.get("forms_of_address", []) or [])
    return [str(x).strip() for x in names if str(x).strip()]


def _item_blob(item: dict) -> str:
    data = item.get("data", {}) if isinstance(item.get("data"), dict) else {}
    return " ".join(_item_names(item)) + " " + json.dumps(data, ensure_ascii=False)


def _profile_view(item: dict) -> dict:
    data = item.get("data", {}) if isinstance(item.get("data"), dict) else {}
    view = {"name": item.get("canonical_key", "")}
    for key in _PROFILE_FIELDS:
        value = data.get(key)
        if value:
            view[key] = value
    return view


def build_context(
    *,
    segments: list[dict],  # [{segment_id, segment_no, segment_type, speaker_name, source_text}]
    scene_card: dict | None,
    knowledge: list[dict],
    style_guide: dict | None,
    carry_over: dict | None,
    storyline: dict | None = None,
    adjacent_prev: str = "",
    adjacent_next: str = "",
    budget: int = DEFAULT_BUDGET,
    reserved_tokens: int = 0,
    lean: bool = False,
) -> ContextResult:
    """组装窗口上下文。

    reserved_tokens 用于 Polish：Draft 块在上下文之外追加，必须先预留其 Token，
    否则知识块会占满预算，最终请求超过模型上下文。
    lean 为上下文撑满时的兜底模式：只保留当前窗口直接命中的必需知识。
    """
    blocks: list[str] = []
    report: dict = {
        "matched_characters": [],
        "matched_terms": [],
        "matched_rules": [],
        "unmatched_characters": [],
        "filtered_future_knowledge": [],
        "filtered_storyline_items": [],
        "trimmed": [],
        "quality": "complete",
        "reserved_tokens": max(0, int(reserved_tokens)),
        "lean": lean,
    }
    snapshot: dict = {
        "knowledge_ids": [],
        "scene_card_id": None,
        "carry_over_hash": None,
        "context_mode": "lean" if lean else "full",
    }

    source_text = "".join(f"[{s['segment_id']}] {s['source_text']}" for s in segments)
    blocks.append("SOURCE SEGMENTS (translate only these, one translation per segment_id):\n" + source_text.rstrip("\n"))

    # 窗口相关性：原句 + 说话人。后续所有知识块都以此为筛选依据。
    window_speakers = {seg.get("speaker_name") for seg in segments if seg.get("speaker_name")}
    window_text = "".join(seg.get("source_text", "") for seg in segments)

    def _mentioned(item: dict) -> bool:
        key = item.get("canonical_key", "")
        if key and key in window_text:
            return True
        for name in _item_names(item)[1:]:
            if name and name in window_text:
                return True
        return key in window_speakers

    chars = [k for k in knowledge if k.get("item_type") == "character" and _mentioned(k)]
    term_records = build_term_records(knowledge)
    term_matches = select_source_matches(term_records, window_text)
    matched_term_ids = {m["record_id"] for m in term_matches}
    term_records_by_id = {r.id: r for r in term_records}
    terms = [
        k for k in knowledge
        if k.get("item_type") == "term"
        and str(k.get("id") or k.get("canonical_key")) in matched_term_ids
    ]

    # 规则/关系不是全局灌入：必须命中窗口文本，或与本窗口人物/术语有关。
    focus_names = set(window_speakers)
    for item in chars + terms:
        focus_names.update(_item_names(item))

    def _related_rule(item: dict) -> bool:
        if _mentioned(item):
            return True
        if lean:
            # 精简模式只保留原文直接提到的规则，避免“相关人物”牵出整片规则库。
            return False
        blob = _item_blob(item)
        return any(name and name in blob for name in focus_names)

    rules = [
        k for k in knowledge
        if k.get("item_type") in ("rule", "relation") and _related_rule(k)
    ]

    if style_guide:
        style_payload = style_guide
        if lean:
            style_payload = {
                key: style_guide.get(key)
                for key in (
                    "dialogue_register",
                    "action_description_style",
                    "capitalization_rules",
                    "cultural_strategy",
                )
                if style_guide.get(key)
            }
        if style_payload:
            blocks.append(_json_block(
                "STYLE GUIDE (frozen, lean)" if lean else "STYLE GUIDE (frozen)",
                style_payload,
            ))
            snapshot["style_guide"] = style_guide.get("_version", 1)

    if scene_card:
        card_body = {k: v for k, v in scene_card.items() if k != "id"}
        blocks.append(_json_block("SCENE CONTEXT CARD", card_body))
        snapshot["scene_card_id"] = scene_card.get("id")
        if scene_card.get("spoiler_guard"):
            blocks.append(
                "[SPOILER GUARD — MUST NOT use or reveal these facts in translation]\n"
                + json.dumps(scene_card["spoiler_guard"], ensure_ascii=False)
            )

    if storyline:
        def _story_related(text: object) -> bool:
            raw = str(text or "")
            return any(name and name in raw for name in focus_names)

        summary = str(storyline.get("summary", "") or "")
        key_events = (
            []
            if lean
            else [x for x in storyline.get("key_events", []) or [] if _story_related(x)]
        )
        # 前情依赖和伏笔属于当前章节连续性的硬约束，即使句中没有出现人名也必须保留；
        # 重要情节与人物状态仍按窗口人物筛选，避免整章剧情全量注入。
        prior_deps = list(storyline.get("prior_deps", []) or [])
        foreshadowing = [] if lean else list(storyline.get("foreshadowing", []) or [])
        states = storyline.get("character_states", {}) or {}
        character_states = {
            name: state for name, state in states.items()
            if any(focus and focus in str(name) for focus in focus_names)
        }
        skipped = (
            len(storyline.get("key_events", []) or []) - len(key_events)
            + len(states) - len(character_states)
        )
        if skipped:
            report["filtered_storyline_items"].append(skipped)
        story_body = {
            "summary": summary,
            "key_events": key_events,
            "prior_deps": prior_deps,
            "character_states": character_states,
        }
        if summary or key_events or prior_deps or character_states or foreshadowing:
            blocks.append(_json_block("CHAPTER STORYLINE (current chapter)", story_body))
            if foreshadowing:
                blocks.append(
                    "[FORESHADOWING — MUST NOT reveal these in translation (spoilers)]\n"
                    + json.dumps(foreshadowing, ensure_ascii=False)
                )
            snapshot["storyline_id"] = storyline.get("id")

    if chars:
        char_rows = []
        for c in chars:
            data = c.get("data", {}) or {}
            char_rows.append({
                "name": c["canonical_key"],
                "aliases": c.get("aliases", []),
                "translation": data.get("translation", ""),
                "role": (
                    data.get("role_translation") or data.get("role")
                    or data.get("identity") or data.get("occupation") or ""
                ),
                "relationships": data.get("relationships", []),
            })
        blocks.append(_json_block("APPROVED CHARACTERS", char_rows))
        alias_map = [
            {
                "character": c["canonical_key"],
                "formal_name": (c.get("data", {}) or {}).get("translation", "") or c["canonical_key"],
                "other_forms": [
                    {"source_form": alias, "note": "按原文使用的称呼翻译，保持同一人物指代一致"}
                    for alias in c.get("aliases", []) or [] if alias
                ],
            }
            for c in chars
            if (c.get("aliases") or []) or (c.get("data", {}) or {}).get("translation")
        ]
        if alias_map and not lean:
            blocks.append(_json_block(
                "NAME-FORM MAP (translate each form as used; keep the same person referentially consistent)",
                alias_map,
            ))
        profiles = [
            _profile_view(c) for c in chars
            if any((c.get("data", {}) or {}).get(k) for k in _PROFILE_FIELDS[1:])
        ]
        if profiles and not lean:
            blocks.append(_json_block("CHARACTER PROFILES (personality & speech style)", profiles))

        # 说话人声音卡：只放窗口内真实说话的人，避免模型拿到无关角色档案后乱套。
        speaker_candidates: dict[str, list[dict]] = {}
        for c in chars:
            for form in [c.get("canonical_key", ""), *(c.get("aliases") or [])]:
                form = str(form or "").strip()
                if form:
                    speaker_candidates.setdefault(normalize(form), []).append(c)
        speaker_index: dict[str, dict] = {}
        for form, candidates in speaker_candidates.items():
            unique = {c.get("id") or c.get("canonical_key"): c for c in candidates}
            if len(unique) == 1:
                speaker_index[form] = next(iter(unique.values()))
        speaker_rows: list[dict] = []
        for speaker in sorted(window_speakers):
            parts = [p.strip() for p in str(speaker).replace("/", "、").split("、") if p.strip()]
            matched_chars: list[dict] = []
            for part in parts or [str(speaker)]:
                c = speaker_index.get(normalize(part))
                if c is not None and c not in matched_chars:
                    matched_chars.append(c)
            for c in matched_chars:
                data = c.get("data", {}) or {}
                cues: list[str] = []
                speaker_forms = {
                    normalize(c.get("canonical_key", "")),
                    *(normalize(a) for a in (c.get("aliases") or [])),
                }
                for seg in segments:
                    if normalize(seg.get("speaker_name") or "") not in speaker_forms:
                        continue
                    for cue in re.findall(r"[（(]([^）)]{1,48})[）)]", seg.get("source_text", "")):
                        cue = cue.strip()
                        if cue and cue not in cues:
                            cues.append(cue)
                    if len(cues) >= 3:
                        break
                if lean:
                    row = {
                        "speaker": c.get("canonical_key", ""),
                        "en_name": data.get("translation") or c.get("canonical_key", ""),
                        "speech_style": data.get("speech_style", ""),
                        "local_emotional_cues": cues,
                    }
                else:
                    row = {
                        "speaker": c.get("canonical_key", ""),
                        "en_name": data.get("translation") or c.get("canonical_key", ""),
                        "personality": data.get("personality", ""),
                        "speech_style": data.get("speech_style", ""),
                        "emotional_range": data.get("emotional_range", ""),
                        "local_emotional_cues": cues,
                    }
                if any(row.get(k) for k in ("personality", "speech_style", "emotional_range", "local_emotional_cues")):
                    speaker_rows.append(row)
        if speaker_rows:
            blocks.append(_json_block(
                "SPEAKER VOICE & LOCAL PERFORMANCE (highest priority for dialogue)",
                speaker_rows,
            ))

        report["matched_characters"] = [c["canonical_key"] for c in chars]
        snapshot["knowledge_ids"] += [c["id"] for c in chars]

    if terms:
        forced_term_ids = {
            m["record_id"] for m in term_matches
            if m.get("policy") == "exact" and m.get("force") and m.get("en")
        }
        forced_terms: list[dict] = []
        contextual_terms: list[dict] = []
        for t in terms:
            rec = term_records_by_id.get(str(t.get("id")))
            if rec is None:
                continue
            row = {
                "term": rec.canonical,
                "translation": rec.en,
                "variants": rec.variants,
            }
            if rec.id in forced_term_ids:
                forced_terms.append(row)
            else:
                row["note"] = (
                    "context-sensitive name form/common noun; translate the form used "
                    "in the source naturally, do not force the canonical wording"
                )
                contextual_terms.append(row)
        if forced_terms:
            blocks.append(_json_block("FORCED TERMS (proper names / titles only)", forced_terms))
        if contextual_terms and not lean:
            blocks.append(_json_block(
                "CONTEXTUAL TERMS (preferred vocabulary, not mandatory)",
                contextual_terms,
            ))
        report["matched_terms"] = [t["canonical_key"] for t in terms]
        snapshot["knowledge_ids"] += [t["id"] for t in terms]

    if rules:
        blocks.append(_json_block(
            "RELEVANT RULES / RELATIONS",
            [{"item": r["canonical_key"], "data": r.get("data", {})} for r in rules],
        ))
        report["matched_rules"] = [r["canonical_key"] for r in rules]
        snapshot["knowledge_ids"] += [r["id"] for r in rules]

    if carry_over:
        blocks.append(_json_block("CARRY-OVER STATE (previous window)", carry_over))
        snapshot["carry_over_hash"] = hashlib.sha256(
            json.dumps(carry_over, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]

    optional: list[tuple[str, str]] = []
    if adjacent_next and not lean:
        optional.append(("adjacent_next", "[NEXT SOURCE (context only, do not translate)]\n" + adjacent_next))
    if adjacent_prev and not lean:
        optional.append(("adjacent_prev", "[PREV SOURCE (context only, do not translate)]\n" + adjacent_prev))

    total = sum(estimate_tokens(b) for b in blocks)
    # 原文是不可裁剪的硬成本；即使预留 Draft 后预算不足，也只裁知识块。
    source_tokens = estimate_tokens(blocks[0])
    effective_budget = max(source_tokens, budget - max(0, int(reserved_tokens)))
    report["effective_budget"] = effective_budget
    for name, text in optional:
        t = estimate_tokens(text)
        if total + t <= effective_budget:
            blocks.append(text)
            total += t
        else:
            report["trimmed"].append(name)

    # 真正执行上下文总预算。当前原文永远保留；超预算时按低优先级逐块裁剪，
    # 而不是把整库知识、规则或相邻原文无限塞给模型。
    trim_order = [
        ("RELEVANT RULES", "rules"),
        ("CONTEXTUAL TERMS", "contextual_terms"),
        ("CHARACTER PROFILES", "character_profiles"),
        ("APPROVED CHARACTERS", "approved_characters"),
        ("NAME-FORM MAP", "name_form_map"),
        ("STYLE GUIDE", "style_guide"),
        ("CHAPTER STORYLINE", "storyline"),
        ("FORESHADOWING", "foreshadowing"),
        ("SPEAKER VOICE", "speaker_voice"),
        ("CARRY-OVER STATE", "carry_over"),
    ]
    while total > effective_budget:
        removed = False
        for prefix, label in trim_order:
            for idx in range(len(blocks) - 1, -1, -1):
                block = blocks[idx]
                if block.startswith("SOURCE SEGMENTS"):
                    continue
                if "[" + prefix in block:
                    total -= estimate_tokens(block)
                    blocks.pop(idx)
                    report["trimmed"].append(label)
                    removed = True
                    break
            if removed:
                break
        if not removed:
            break

    prompt_text = "\n".join(blocks)
    return ContextResult(
        prompt_text=prompt_text,
        report=report,
        snapshot=snapshot,
        total_tokens=total,
    )


def build_qa_reference(
    *,
    segments: list[dict],
    knowledge: list[dict],
    storyline: dict | None = None,
    max_chars: int = 2600,
) -> str:
    """构建语义 QA 的紧凑事实基准。

    只包含本窗口相关角色、专名与当前章节剧情；不包含整库知识，也不要求逐字措辞。
    """
    window_text = "".join(seg.get("source_text", "") for seg in segments)
    speakers = {str(seg.get("speaker_name") or "").strip() for seg in segments}
    speaker_map = build_speaker_map(knowledge)
    records = build_term_records(knowledge)
    matches = select_source_matches(records, window_text)

    characters: list[dict] = []
    for item in knowledge:
        if item.get("item_type") != "character":
            continue
        names = [item.get("canonical_key", ""), *(item.get("aliases") or [])]
        mentioned = any(name and name in window_text for name in names)
        is_speaker = any(normalize(sp) in {normalize(n) for n in names if n} for sp in speakers)
        if not mentioned and not is_speaker:
            continue
        data = item.get("data", {}) or {}
        characters.append({
            "source_name": item.get("canonical_key", ""),
            "english_name": data.get("translation") or speaker_map.get(normalize(item.get("canonical_key", "")), ""),
            "aliases": item.get("aliases", []),
            "forms_of_address": data.get("forms_of_address") or [],
            "role": (
                data.get("role_translation") or data.get("role")
                or data.get("identity") or data.get("occupation") or ""
            ),
            "relationships": data.get("relationships") or [],
        })

    terms = [
        {
            "source": m["source_form"],
            "canonical": m["canonical"],
            "english": m["en"],
            "exact": m["policy"] == "exact" and bool(m.get("force")),
        }
        for m in matches
        if m.get("en")
    ]
    # 同一词形/译名只保留一条。
    dedup_terms: list[dict] = []
    seen_terms: set[tuple[str, str]] = set()
    for term in terms:
        key = (normalize(term["source"]), normalize(term["english"]))
        if key not in seen_terms:
            seen_terms.add(key)
            dedup_terms.append(term)

    # QA 需要人物关系/称呼/身份事实，否则只能看到人名，无法判断 "aunt" 是否该是 "mother"。
    focus_names = {str(s).strip() for s in speakers if str(s).strip()}
    for c in characters:
        for name in (
            c.get("source_name", ""), c.get("english_name", ""),
            *(c.get("aliases") or []), *(c.get("forms_of_address") or []),
        ):
            name = str(name or "").strip()
            if len(name) >= 2:
                focus_names.add(name)
    for m in matches:
        for name in (m.get("source_form", ""), m.get("canonical", ""), m.get("en", "")):
            name = str(name or "").strip()
            if len(name) >= 2:
                focus_names.add(name)

    def _fact_relevant(item: dict) -> bool:
        names = [n for n in _item_names(item) if len(n) >= 2]
        if any(n in window_text for n in names):
            return True
        blob = _item_blob(item)
        return any(n in blob for n in focus_names)

    relations = [
        {"item": item.get("canonical_key", ""), "data": item.get("data", {}) or {}}
        for item in knowledge
        if item.get("item_type") == "relation" and _fact_relevant(item)
    ][:16]
    rules = [
        {"item": item.get("canonical_key", ""), "data": item.get("data", {}) or {}}
        for item in knowledge
        if item.get("item_type") == "rule" and _fact_relevant(item)
    ][:12]

    story = {}
    if storyline:
        story = {
            "summary": storyline.get("summary", ""),
            "key_events": storyline.get("key_events", [])[:10],
            "prior_deps": storyline.get("prior_deps", [])[:8],
            "character_states": storyline.get("character_states", {}),
            "foreshadowing": storyline.get("foreshadowing", [])[:6],
        }
    payload = {
        "characters": characters[:16],
        "terms": dedup_terms[:30],
        "relationships": relations,
        "translation_rules": rules,
        "chapter_storyline": story,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > max_chars:
        # 优先保留角色、关系与专名；剧情卡只保留摘要与前情依赖。
        story = {k: story.get(k) for k in ("summary", "prior_deps") if story.get(k)}
        payload["chapter_storyline"] = story
        payload["translation_rules"] = rules[:6]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text[:max_chars]
