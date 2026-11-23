"""Context Builder 单元测试。"""

from app.services.context_builder import build_context


def test_assembly_order_and_snapshot() -> None:
    segs = [
        {"segment_id": "S1", "segment_no": 1, "segment_type": "dialogue",
         "speaker_name": "林晓", "source_text": "天机阁的秘密，你好。\n"},
    ]
    result = build_context(
        segments=segs,
        scene_card={"id": "C1", "location": "咖啡厅", "spoiler_guard": ["林晓是卧底"]},
        knowledge=[
            {"id": "K1", "item_type": "character", "canonical_key": "林晓", "data": {}},
            {"id": "K2", "item_type": "term", "canonical_key": "天机阁", "data": {"translation": "Tianji Pavilion"}},
        ],
        style_guide={"dialogue_register": "natural"},
        carry_over={"ongoing_state": "tension"},
        budget=4000,
    )
    text = result.prompt_text
    # 组装顺序：原文 → 风格基线 → 场景卡 → spoiler guard → 人物 → 术语 → carry-over
    assert text.index("SOURCE SEGMENTS") < text.index("STYLE GUIDE")
    assert text.index("STYLE GUIDE") < text.index("SCENE CONTEXT CARD")
    assert text.index("SCENE CONTEXT CARD") < text.index("SPOILER GUARD")
    assert text.index("APPROVED CHARACTERS") < text.index("FORCED TERMS")
    assert "CARRY-OVER STATE" in text
    assert "MUST NOT use" in text
    assert result.snapshot["knowledge_ids"] == ["K1", "K2"]
    assert result.snapshot["scene_card_id"] == "C1"


def test_unrelated_knowledge_excluded() -> None:
    """按需匹配：窗口未出现的角色/术语不进上下文。"""
    result = build_context(
        segments=[{"segment_id": "S1", "segment_no": 1, "segment_type": "narrative",
                   "speaker_name": None, "source_text": "深夜，街头无人。"}],
        scene_card=None,
        knowledge=[
            {"id": "K1", "item_type": "character", "canonical_key": "林晓", "data": {}},
            {"id": "K2", "item_type": "term", "canonical_key": "天机阁", "data": {"translation": "Tianji Pavilion"}},
        ],
        style_guide=None,
        carry_over=None,
    )
    assert "APPROVED CHARACTERS" not in result.prompt_text
    assert "FORCED TERMS" not in result.prompt_text
    assert result.snapshot["knowledge_ids"] == []


def test_budget_trim_drops_adjacent_not_core() -> None:
    segs = [{"segment_id": "S1", "segment_no": 1, "segment_type": "dialogue",
             "speaker_name": None, "source_text": "x" * 100}]
    big_next = "y" * 30000
    result = build_context(
        segments=segs,
        scene_card={"location": "L"},
        knowledge=[],
        style_guide=None,
        carry_over=None,
        adjacent_next=big_next,
        budget=3000,
    )
    assert "NEXT SOURCE" not in result.prompt_text
    assert result.report["trimmed"] == ["adjacent_next"]
    assert "SOURCE SEGMENTS" in result.prompt_text  # 原文永不被裁


def test_polish_context_includes_profile_storyline_and_carry() -> None:
    """Polish 上下文必须包含角色档案、当前章节剧情与 carry-over。"""
    result = build_context(
        segments=[{
            "segment_id": "S1", "segment_no": 1, "segment_type": "dialogue",
            "speaker_name": "林晓", "source_text": "你终于来了。\n",
        }],
        scene_card=None,
        knowledge=[{
            "id": "K1", "item_type": "character", "canonical_key": "林晓",
            "aliases": ["小林"], "data": {
                "translation": "Lin Xiao",
                "personality": "沉稳内敛",
                "speech_style": "短句、克制、少寒暄",
                "forms_of_address": ["小林"],
            },
        }],
        style_guide={"dialogue_register": "natural"},
        carry_over={"ongoing_state": "两人刚重逢", "open_threads": ["旧案"]},
        storyline={
            "id": "ST1", "summary": "重逢", "key_events": ["林晓出现"],
            "prior_deps": ["上一集分别"], "character_states": {"林晓": "保持警惕"},
        },
    )
    text = result.prompt_text
    assert "CHARACTER PROFILES (personality & speech style)" in text
    assert "沉稳内敛" in text
    assert "短句、克制、少寒暄" in text
    assert "CHAPTER STORYLINE (current chapter)" in text
    assert "上一集分别" in text
    assert "CARRY-OVER STATE (previous window)" in text
    assert "两人刚重逢" in text
    assert result.report["matched_characters"] == ["林晓"]
