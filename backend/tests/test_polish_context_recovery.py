"""Polish 上下文超限恢复：多段自动拆窗，单段降级极简上下文。"""

import asyncio
import json
import re
from types import SimpleNamespace

from app.llm.fake import FakeProvider
from app.llm.protocol import LLMError
from app.llm.registry import set_provider
from app.services.context_builder import ContextResult
from app.services.translation import validate_window
import app.services.translation_batch as batch


def _seg(idx: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{idx:032x}",
        segment_no=idx,
        segment_type="narrative",
        speaker_name=None,
        source_text=text,
        chapter_id="c1",
        scene_id="s1",
        source_hash=f"hash-{idx}",
    )


def _install_material(monkeypatch):
    seen: dict = {"lean_calls": []}

    async def fake_material(
        db_factory, project_id, window, carry_over, *, reserved_tokens=0, lean=False
    ):
        seen["reserved_tokens"] = reserved_tokens
        seen["lean_calls"].append(lean)
        seg_dicts = [
            {
                "segment_id": s.id,
                "segment_no": s.segment_no,
                "segment_type": s.segment_type,
                "speaker_name": s.speaker_name,
                "source_text": s.source_text,
                "line_count_optional": False,
            }
            for s in window
        ]
        return (
            seg_dicts,
            ContextResult(
                prompt_text=(
                    "SOURCE SEGMENTS (translate only these, one translation per segment_id):\n"
                    + "\n".join(f"[{s.id}] {s.source_text}" for s in window)
                )
            ),
            None,
            None,
        )

    async def fake_canonicalize(db_factory, project_id, window, polished):
        return polished

    monkeypatch.setattr(batch, "_window_material", fake_material)
    monkeypatch.setattr(batch, "_canonicalize_polished_terms", fake_canonicalize)
    return seen


def _polish_response(content: str) -> dict:
    ids = re.findall(r"\[([0-9a-f]{32})\]", content)
    return {
        "window_id": "W",
        "translations": [
            {"segment_id": sid, "target_text": f"EN {sid[-4:]}", "notes": []}
            for sid in dict.fromkeys(ids)
        ],
        "carry_over": {
            "forms_of_address": [],
            "ongoing_state": "继续",
            "open_threads": [],
            "active_expressions": [],
        },
    }


def test_context_length_retries_with_lean_before_split(monkeypatch):
    seen = _install_material(monkeypatch)
    provider = FakeProvider()
    state = {"calls": 0}

    def handler(messages):
        state["calls"] += 1
        content = messages[-1]["content"]
        if state["calls"] == 1:
            raise LLMError("模拟上下文超限", retryable=False, code="context_length")
        return _polish_response(content)

    set_provider(provider.on(handler))
    window = [_seg(i, f"第{i}段动作。") for i in range(1, 5)]
    draft = [{"segment_id": s.id, "target_text": f"Action {s.segment_no}."} for s in window]

    polished, _carry = asyncio.run(
        batch.translate_window_polish_direct(lambda: None, "p1", window, draft, None, "m")
    )

    # 完整上下文失败后，同一窗口先用精简上下文成功，无需拆窗。
    assert state["calls"] == 2
    assert seen["lean_calls"] == [False, True]
    assert {e["segment_id"] for e in polished} == {s.id for s in window}
    assert seen["reserved_tokens"] > 0
    assert validate_window(
        [
            {"segment_id": s.id, "segment_type": s.segment_type,
             "source_text": s.source_text, "line_count_optional": False}
            for s in window
        ],
        polished,
    ) == []


def test_lean_failure_still_splits_multi_segment_window(monkeypatch):
    seen = _install_material(monkeypatch)
    provider = FakeProvider()
    state = {"calls": 0}

    def handler(messages):
        state["calls"] += 1
        content = messages[-1]["content"]
        if state["calls"] <= 2:
            raise LLMError("模拟上下文超限", retryable=False, code="context_length")
        return _polish_response(content)

    set_provider(provider.on(handler))
    window = [_seg(i, f"第{i}段动作。") for i in range(1, 5)]
    draft = [{"segment_id": s.id, "target_text": f"Action {s.segment_no}."} for s in window]

    polished, _carry = asyncio.run(
        batch.translate_window_polish_direct(lambda: None, "p1", window, draft, None, "m")
    )

    # 完整/精简都失败后，两个半窗正常完成。
    assert state["calls"] == 4
    assert seen["lean_calls"] == [False, True, False, False]
    assert {e["segment_id"] for e in polished} == {s.id for s in window}


def test_single_segment_context_length_uses_minimal_after_lean(monkeypatch):
    seen = _install_material(monkeypatch)
    provider = FakeProvider()
    state = {"calls": 0}

    def handler(messages):
        state["calls"] += 1
        content = messages[-1]["content"]
        if state["calls"] <= 2:
            raise LLMError("模拟上下文超限", retryable=False, code="context_length")
        return _polish_response(content)

    set_provider(provider.on(handler))
    window = [_seg(1, "最后一段。")]
    draft = [{"segment_id": window[0].id, "target_text": "Final line."}]

    polished, _carry = asyncio.run(
        batch.translate_window_polish_direct(lambda: None, "p1", window, draft, None, "m")
    )

    assert state["calls"] == 3
    assert seen["lean_calls"] == [False, True]
    assert polished[0]["segment_id"] == window[0].id
    assert "CARRY-OVER STATE" not in json.dumps(polished, ensure_ascii=False)
