"""Context Builder：按窗口相关性组装上下文，生成 Report 与快照。

原则：不把整本知识库塞进 Prompt。角色、术语、规则/关系、章节剧情都按当前
窗口原句、说话人、别名和称呼做相关性筛选；Polish 只收到本窗口需要的内容。
"""

import hashlib
import json
from dataclasses import dataclass, field

from app.utils.token_budget import estimate_tokens

DEFAULT_BUDGET = 6000
_PROFILE_FIELDS = (
    "translation",
    "personality",
    "speech_style",
    "forms_of_address",
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
) -> ContextResult:
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
    }
    snapshot: dict = {"knowledge_ids": [], "scene_card_id": None, "carry_over_hash": None}

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
    terms = [k for k in knowledge if k.get("item_type") == "term" and _mentioned(k)]

    # 规则/关系不是全局灌入：必须命中窗口文本，或与本窗口人物/术语有关。
    focus_names = set(window_speakers)
    for item in chars + terms:
        focus_names.update(_item_names(item))

    def _related_rule(item: dict) -> bool:
        if _mentioned(item):
            return True
        blob = _item_blob(item)
        return any(name and name in blob for name in focus_names)

    rules = [
        k for k in knowledge
        if k.get("item_type") in ("rule", "relation") and _related_rule(k)
    ]

    if style_guide:
        blocks.append(_json_block("STYLE GUIDE (frozen)", style_guide))
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
        key_events = [x for x in storyline.get("key_events", []) or [] if _story_related(x)]
        # 前情依赖和伏笔属于当前章节连续性的硬约束，即使句中没有出现人名也必须保留；
        # 重要情节与人物状态仍按窗口人物筛选，避免整章剧情全量注入。
        prior_deps = list(storyline.get("prior_deps", []) or [])
        foreshadowing = list(storyline.get("foreshadowing", []) or [])
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
        char_rows = [
            {
                "name": c["canonical_key"],
                "aliases": c.get("aliases", []),
                "translation": (c.get("data", {}) or {}).get("translation", ""),
            }
            for c in chars
        ]
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
        if alias_map:
            blocks.append(_json_block(
                "NAME-FORM MAP (translate each form as used; keep the same person referentially consistent)",
                alias_map,
            ))
        profiles = [
            _profile_view(c) for c in chars
            if any((c.get("data", {}) or {}).get(k) for k in _PROFILE_FIELDS[1:])
        ]
        if profiles:
            blocks.append(_json_block("CHARACTER PROFILES (personality & speech style)", profiles))
        report["matched_characters"] = [c["canonical_key"] for c in chars]
        snapshot["knowledge_ids"] += [c["id"] for c in chars]

    if terms:
        forced_terms = [
            {
                "term": t["canonical_key"],
                "translation": t.get("data", {}).get("translation"),
                "variants": t.get("data", {}).get("variants") or [],
            }
            for t in terms
            if t.get("data", {}).get("translation")
        ]
        ref_terms = [t["canonical_key"] for t in terms if not t.get("data", {}).get("translation")]
        if forced_terms:
            blocks.append(_json_block("FORCED TERMS", forced_terms))
        if ref_terms:
            blocks.append(_json_block("REFERENCE TERMS (background, not mandatory)", ref_terms))
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
    if adjacent_next:
        optional.append(("adjacent_next", "[NEXT SOURCE (context only, do not translate)]\n" + adjacent_next))
    if adjacent_prev:
        optional.append(("adjacent_prev", "[PREV SOURCE (context only, do not translate)]\n" + adjacent_prev))

    total = sum(estimate_tokens(b) for b in blocks)
    for name, text in optional:
        t = estimate_tokens(text)
        if total + t <= budget:
            blocks.append(text)
            total += t
        else:
            report["trimmed"].append(name)

    prompt_text = "\n".join(blocks)
    return ContextResult(
        prompt_text=prompt_text,
        report=report,
        snapshot=snapshot,
        total_tokens=total,
    )
