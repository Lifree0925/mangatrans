"""对白质量合同测试：防止自然度、范围、关系与术语规则被后续改动回退。"""

from app.services.knowledge_service import EXTRACT_PROMPT
from app.services.qa_service import SEMANTIC_PROMPT
from app.services.translation import (
    DRAFT_SYSTEM,
    POLISH_SYSTEM,
    PROMPT_VERSION,
)


def test_prompt_version_is_bumped_for_quality_contract() -> None:
    assert PROMPT_VERSION == "v4"


def test_draft_prompt_locks_scope_relations_and_metadata() -> None:
    assert "hasn't been there in five years" in DRAFT_SYSTEM
    assert "never \"has never been there\"" in DRAFT_SYSTEM
    assert "Never substitute a different relative" in DRAFT_SYSTEM
    assert "HEADINGS AND METADATA ARE NOT DIALOGUE" in DRAFT_SYSTEM
    assert "Mr. Smith" in DRAFT_SYSTEM
    assert "use\n  that relationship" in DRAFT_SYSTEM
    assert "a speaker normally refers to themselves as I/me/my" in DRAFT_SYSTEM


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
    assert "不报告风格、语气、用词偏好" in SEMANTIC_PROMPT


def test_knowledge_prompt_extracts_explicit_relationships_and_roles() -> None:
    assert "item_type=relation" in EXTRACT_PROMPT
    assert "relation_translation" in EXTRACT_PROMPT
    assert "data.role_translation" in EXTRACT_PROMPT
    assert "没有明确证据时不要输出" in EXTRACT_PROMPT


def test_polish_prompt_contains_performance_and_subtext_rules() -> None:
    flat = " ".join(POLISH_SYSTEM.split())
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
