"""对白质量合同测试：防止自然度、范围、关系与术语规则被后续改动回退。"""

from app.services.knowledge_service import EXTRACT_PROMPT
from app.services.qa_service import SEMANTIC_PROMPT, build_coverage_signals
from app.services.storyline_service import PROMPT as STORYLINE_PROMPT
from app.services.translation import (
    DRAFT_SYSTEM,
    POLISH_SYSTEM,
    PROMPT_VERSION,
    REPAIR_SYSTEM,
)


def test_prompt_version_is_bumped_for_quality_contract() -> None:
    assert PROMPT_VERSION == "v7"


def test_draft_prompt_locks_scope_relations_and_metadata() -> None:
    assert "hasn't been there in five years" in DRAFT_SYSTEM
    assert "never \"has never been there\"" in DRAFT_SYSTEM
    assert "Never substitute a different relative" in DRAFT_SYSTEM
    assert "HEADINGS AND METADATA ARE NOT DIALOGUE" in DRAFT_SYSTEM
    assert "Mr. Smith" in DRAFT_SYSTEM
    assert "use\n  that relationship" in DRAFT_SYSTEM
    assert "a speaker normally refers to themselves as I/me/my" in DRAFT_SYSTEM


def test_polish_prompt_requires_no_change_default() -> None:
    assert "NO-CHANGE DEFAULT" in POLISH_SYSTEM
    assert "copy it verbatim" in POLISH_SYSTEM
    assert "A stylistic preference is not a defect" in POLISH_SYSTEM
    assert "REPAIR RULES" in REPAIR_SYSTEM
    assert "Do not perform a general polish" in REPAIR_SYSTEM


def test_polish_prompt_requires_english_first_dialogue_localization() -> None:
    assert "DIALOGUE LOCALIZATION" in POLISH_SYSTEM
    assert "Leave us alone" in POLISH_SYSTEM
    assert "bargain-bin charm" in POLISH_SYSTEM
    assert "That winery basement last night?" in POLISH_SYSTEM
    assert "a kept woman who needs a man to support her" in POLISH_SYSTEM
    assert "African strongman" in POLISH_SYSTEM
    assert "a neurotoxin is not a \"Neural Pill\"" in POLISH_SYSTEM
    assert "I haven't been there in five years" in POLISH_SYSTEM
    assert 'project facts say "mother"' in POLISH_SYSTEM
    assert 'do not write "aunt"' in POLISH_SYSTEM


def test_semantic_qa_checks_scope_and_relationship_not_style() -> None:
    assert "时间/频率/数量范围" in SEMANTIC_PROMPT
    assert "亲属关系、社会角色或称呼" in SEMANTIC_PROMPT
    assert "把“五年没去”译成“从未去过”是范围扩大" in SEMANTIC_PROMPT
    assert "不把风格、语气、用词偏好" in SEMANTIC_PROMPT
    assert "source-first 两遍法" in SEMANTIC_PROMPT
    assert "[NATURALNESS TABLE READ" in SEMANTIC_PROMPT
    assert "naturalness_edits 不是 QA issue" in SEMANTIC_PROMPT
    assert "reason_category" in SEMANTIC_PROMPT
    assert "confidence >= 0.95" in SEMANTIC_PROMPT
    assert "每窗最多 3 条" in SEMANTIC_PROMPT
    assert "[DETERMINISTIC COVERAGE SIGNALS]" in SEMANTIC_PROMPT


def test_coverage_signals_flag_negation_condition_and_numbers() -> None:
    segments = [
        {
            "segment_id": "s1",
            "source_text": "如果不去，我就不会回来，金库只剩99%。",
        }
    ]
    hint, stats = build_coverage_signals(segments, {"s1": "I will come back when the vault opens."})
    assert "否定" in hint
    assert "条件" in hint
    assert "99" in hint
    assert stats == {"checked": 1, "negation": 1, "condition": 1, "number": 1}

    ok_hint, ok_stats = build_coverage_signals(
        [{"segment_id": "s2", "source_text": "如果没有钱，我就不能走。"}],
        {"s2": "If I have no money, I can't leave."},
    )
    assert not ok_hint.startswith("- [s2]")
    assert ok_stats["negation"] == 0


def test_knowledge_prompt_extracts_explicit_relationships_and_roles() -> None:
    assert "item_type=relation" in EXTRACT_PROMPT
    assert "relation_translation" in EXTRACT_PROMPT
    assert "data.role_translation" in EXTRACT_PROMPT
    assert "没有明确证据时不要输出" in EXTRACT_PROMPT


def test_storyline_prompt_captures_structured_character_state() -> None:
    assert "character_state_details" in STORYLINE_PROMPT
    assert "known_secrets" in STORYLINE_PROMPT
    assert "不要把后文才揭晓的信息提前写入" in STORYLINE_PROMPT
    assert "critical_facts" in STORYLINE_PROMPT
    assert "relationship/identity/time/location/possession" in STORYLINE_PROMPT


def test_polish_prompt_contains_performance_and_subtext_rules() -> None:
    flat = " ".join(POLISH_SYSTEM.split())
    assert "DRAFT IS A SEMANTIC ANCHOR, NOT A STYLE TEMPLATE" in flat
    assert "AGENCY & DIRECTION" in flat
    assert "PERFORMANCE RHYTHM & SUBTEXT" in flat
    assert "Even if it kills me. Try me." in flat
    assert "That fearless, confident woman is still here. I see her." in flat
    assert "I screwed up" in flat
    assert "Easy with the gun" in flat
    assert "I came here to protect you" in flat
    assert "I've got you" in flat
    assert "Thank you for staying alive. For giving me the chance to tell you I love you." in flat
    assert "very different kind of woman. I'm something new. It'll wear off." in flat
    assert "do not replace it with the character's name" in flat
    assert "NATIVE DIALOGUE AUDIT" in flat
    assert "Naturalness never overrides facts or continuity" in flat
