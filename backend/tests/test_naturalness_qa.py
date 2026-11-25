"""QA 台词自然度修订的护栏与 Prompt 契约。"""

from app.prompts.script_translation_prompt import thinking_standard_block
from app.services import qa_service
from app.services.translation import POLISH_SYSTEM


def _seg(sid: str = "a" * 32, *, segment_type: str = "dialogue") -> dict:
    return {
        "segment_id": sid,
        "segment_type": segment_type,
        "speaker_name": "Chloe",
        "source_text": "Chloe（冷笑）：你学习的只是五年前的我！现在让你看看这四年我是如何教导我儿子的！",
    }


def test_prompt_defines_draft_as_fact_anchor_and_native_audit() -> None:
    assert "DRAFT IS A SEMANTIC ANCHOR, NOT A STYLE TEMPLATE" in POLISH_SYSTEM
    assert "AGENCY & DIRECTION" in POLISH_SYSTEM
    standard = thinking_standard_block()
    assert "FIDELITY OPERATIONAL NOTE" in standard
    assert "does NOT mean preserving Chinese syntax" in standard


def test_semantic_output_accepts_naturalness_edits() -> None:
    parsed = qa_service.SemanticOutput.model_validate({
        "issues": [],
        "naturalness_edits": [{
            "segment_id": "a" * 32,
            "target_quote": "What you learned is only the me from five years ago.",
            "suggested_text": "You only know who I was five years ago.",
            "reason": "中文话题结构",
            "confidence": 0.95,
        }],
    })
    assert parsed.naturalness_edits[0].confidence == 0.95


def test_naturalness_guard_rejects_meaning_changes() -> None:
    source = "我五年前没去迈阿密。"
    old = "I haven't been to Miami in five years."
    ok, reason = qa_service.naturalness_edit_guard(
        source_text=source, old_text=old, new_text="I've never been to Miami.",
        forced_terms={},
    )
    # 时间范围扩大同时丢了 five，确定性护栏直接拒绝，不允许静默改义。
    assert not ok and reason == "number"

    ok, reason = qa_service.naturalness_edit_guard(
        source_text="你必须马上离开。", old_text="You have to leave now.",
        new_text="You don't have to leave now.", forced_terms={},
    )
    assert not ok and reason == "negation"

    ok, reason = qa_service.naturalness_edit_guard(
        source_text="给我五个。", old_text="Give me five.",
        new_text="Give me six.", forced_terms={},
    )
    assert not ok and reason == "number"


def test_naturalness_guard_preserves_forced_term() -> None:
    sid = "a" * 32
    forced = {
        "潘多拉": {
            "source": "潘多拉",
            "en": "Pandora",
            "variants": [],
            "policy": "exact",
            "force": True,
        }
    }
    ok, reason = qa_service.naturalness_edit_guard(
        source_text="潘多拉正在攻击核心数据库。",
        old_text="Pandora is attacking the core database.",
        new_text="The virus is attacking the core database.",
        forced_terms=forced,
    )
    assert not ok and reason.startswith("forced_term:")

    accepted = qa_service.select_naturalness_edits(
        segments=[{
            "segment_id": sid,
            "segment_type": "dialogue",
            "speaker_name": "Chloe",
            "source_text": "潘多拉正在攻击核心数据库。",
        }],
        targets={sid: "Pandora is attacking the core database."},
        edits=[{
            "segment_id": sid,
            "target_quote": "Pandora is attacking the core database.",
            "suggested_text": "Pandora's hitting the core database right now.",
            "confidence": 0.95,
        }],
        forced_terms=forced,
    )
    assert accepted == {sid: "Pandora's hitting the core database right now."}


def test_select_naturalness_edits_requires_quote_and_dialogue() -> None:
    sid = "b" * 32
    seg = _seg(sid)
    old = "Chloe (smirks): What you learned is only the me from five years ago."
    edit = {
        "segment_id": sid,
        "target_quote": old,
        "suggested_text": "Chloe (smirks): You only know who I was five years ago.",
        "confidence": 0.95,
    }
    assert qa_service.select_naturalness_edits(
        segments=[seg], targets={sid: old}, edits=[edit], forced_terms={},
    ) == {sid: edit["suggested_text"]}

    bad_quote = {**edit, "target_quote": "not in target"}
    assert qa_service.select_naturalness_edits(
        segments=[seg], targets={sid: old}, edits=[bad_quote], forced_terms={},
    ) == {}

    narrative = {**_seg(sid), "segment_type": "narrative"}
    assert qa_service.select_naturalness_edits(
        segments=[narrative], targets={sid: old}, edits=[edit], forced_terms={},
    ) == {}
