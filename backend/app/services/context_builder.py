"""Context Builder：按文档 14 章固定顺序组装上下文，生成 Report 与快照。"""

import hashlib
import json
from dataclasses import dataclass, field

from app.utils.token_budget import estimate_tokens

DEFAULT_BUDGET = 6000


@dataclass
class ContextResult:
    prompt_text: str
    report: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    quality: str = "complete"  # complete / partial / needs_review
    total_tokens: int = 0


def _json_block(title: str, payload: dict | str) -> str:
    if isinstance(payload, dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        body = str(payload)
    return f"\n\n[{title}]\n{body}\n"


def build_context(
    *,
    segments: list[dict],  # [{segment_id, segment_no, segment_type, speaker_name, source_text}]
    scene_card: dict | None,
    knowledge: list[dict],
    style_guide: dict | None,
    carry_over: dict | None,
    adjacent_prev: str = "",
    adjacent_next: str = "",
    budget: int = DEFAULT_BUDGET,
) -> ContextResult:
    blocks: list[str] = []
    report: dict = {
        "matched_characters": [],
        "matched_terms": [],
        "unmatched_characters": [],
        "filtered_future_knowledge": [],
        "trimmed": [],
        "quality": "complete",
    }
    snapshot: dict = {"knowledge_ids": [], "scene_card_id": None, "carry_over_hash": None}

    source_text = "".join(f"[{s['segment_id']}] {s['source_text']}" for s in segments)
    blocks.append("SOURCE SEGMENTS (translate only these, one translation per segment_id):\n" + source_text.rstrip("\n"))

    if style_guide:
        blocks.append(_json_block("STYLE GUIDE (frozen)", style_guide))
        snapshot["style_guide"] = style_guide.get("_version", 1)

    if scene_card:
        card_body = {k: v for k, v in scene_card.items() if k != "id"}
        blocks.append(_json_block("SCENE CONTEXT CARD", card_body))
        snapshot["scene_card_id"] = scene_card.get("id")
        # spoiler_guard 信息必须注明"禁止使用"
        if scene_card.get("spoiler_guard"):
            blocks.append(
                "[SPOILER GUARD — MUST NOT use or reveal these facts in translation]\n"
                + json.dumps(scene_card["spoiler_guard"], ensure_ascii=False)
            )

    chars = [k for k in knowledge if k.get("item_type") == "character"]
    terms = [k for k in knowledge if k.get("item_type") == "term"]
    rules = [k for k in knowledge if k.get("item_type") in ("rule", "relation")]
    if chars:
        blocks.append(
            _json_block(
                "APPROVED CHARACTERS",
                [{"name": c["canonical_key"], "data": c.get("data", {})} for c in chars],
            )
        )
        report["matched_characters"] = [c["canonical_key"] for c in chars]
        snapshot["knowledge_ids"] += [c["id"] for c in chars]
    if terms:
        blocks.append(
            _json_block(
                "FORCED TERMS",
                [{"term": t["canonical_key"], "translation": t.get("data", {})} for t in terms],
            )
        )
        report["matched_terms"] = [t["canonical_key"] for t in terms]
        snapshot["knowledge_ids"] += [t["id"] for t in terms]
    if rules:
        blocks.append(
            _json_block(
                "TRANSLATION RULES",
                [{"rule": r["canonical_key"], "data": r.get("data", {})} for r in rules],
            )
        )
        snapshot["knowledge_ids"] += [r["id"] for r in rules]

    if carry_over:
        blocks.append(_json_block("CARRY-OVER STATE (previous window)", carry_over))
        snapshot["carry_over_hash"] = hashlib.sha256(
            json.dumps(carry_over, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]

    # 预算裁剪顺序：相邻原文 → 非关键知识（rules）→ 场景卡不可裁
    optional: list[tuple[str, str]] = []
    if adjacent_next:
        optional.append(("adjacent_next", "[NEXT SOURCE (context only, do not translate)]\n" + adjacent_next))
    if adjacent_prev:
        optional.append(("adjacent_prev", "[PREV SOURCE (context only, do not translate)]\n" + adjacent_prev))

    # 估算当前总量
    total = sum(estimate_tokens(b) for b in blocks)
    for name, text in optional:
        t = estimate_tokens(text)
        if total + t <= budget:
            blocks.append(text)
            total += t
        else:
            report["trimmed"].append(name)

    prompt_text = "\n".join(blocks)
    result = ContextResult(
        prompt_text=prompt_text,
        report=report,
        snapshot=snapshot,
        total_tokens=total,
    )
    return result
