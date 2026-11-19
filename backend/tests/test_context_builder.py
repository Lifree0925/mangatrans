"""Context Builder 单元测试。"""

from app.services.context_builder import build_context


def test_assembly_order_and_snapshot() -> None:
    segs = [
        {"segment_id": "S1", "segment_no": 1, "segment_type": "dialogue",
         "speaker_name": "林晓", "source_text": "你好。\n"},
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
