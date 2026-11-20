"""LLM 输出 JSON 容错解析测试。"""

import pytest

from app.llm.json_utils import parse_llm_json


def test_plain_json():
    assert parse_llm_json('{"a": 1}') == {"a": 1}


def test_markdown_fence():
    assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_surrounding_text_and_trailing_comma():
    assert parse_llm_json('好的，结果如下：\n{"items": [{"k": 1,},],}\n希望有帮助') == {
        "items": [{"k": 1}]
    }


def test_array():
    assert parse_llm_json('["x", "y"]') == ["x", "y"]


def test_invalid_raises():
    with pytest.raises(ValueError):
        parse_llm_json("完全不是 JSON 的文本")


def test_empty_raises():
    with pytest.raises(ValueError):
        parse_llm_json("")
