"""每窗口 append-only 会话：Polish 复用 Draft 的上下文前缀。"""

from app.llm.fake import FakeProvider
from app.llm.protocol import ProviderCapabilities
from app.prompts.script_translation_prompt import THINKING_STANDARD_MARKER
from app.services.context_builder import ContextResult
from app.services.qa_service import semantic_qa
from app.services.translation import (
    POLISH_STAGE_INSTRUCTION,
    translate_window_draft,
    translate_window_polish,
)


def _conversation_provider() -> FakeProvider:
    provider = FakeProvider()
    provider.capabilities = ProviderCapabilities(
        supports_json_object=True,
        context_window=128_000,
        supports_conversation_cache=True,
    )

    def handler(messages):
        last = messages[-1].get("content", "") if messages else ""
        all_text = "\n".join(str(m.get("content", "")) for m in messages)
        if last.startswith("STAGE: DRAFT"):
            return {
                "window_id": "W1",
                "translations": [{
                    "segment_id": "S1",
                    "target_text": "Draft line.",
                    "notes": [],
                    "ambiguity": [],
                }],
                "carry_over": {"ongoing_state": "tension"},
            }
        if last.startswith("STAGE: POLISH") or "[FAITHFUL DRAFT]" in all_text:
            return {
                "window_id": "W1",
                "translations": [{
                    "segment_id": "S1",
                    "target_text": "Natural line.",
                    "notes": [],
                    "ambiguity": [],
                }],
                "carry_over": {"ongoing_state": "resolved"},
            }
        return None

    provider.handlers.append(handler)
    return provider


def test_polish_reuses_draft_context_in_append_only_conversation() -> None:
    provider = _conversation_provider()
    context = ContextResult(
        prompt_text=(
            "PROJECT BIBLE\nSTABLE REFERENCE\n\n"
            "SOURCE SEGMENTS (translate only these, one translation per segment_id):\n"
            "[S1] 你终于来了。"
        )
    )
    segments = [{
        "segment_id": "S1",
        "segment_no": 1,
        "segment_type": "dialogue",
        "speaker_name": "林晓",
        "source_text": "你终于来了。",
    }]
    draft, draft_carry = __import__("asyncio").run(
        translate_window_draft(
            provider=provider,
            model="mock",
            window_id="W1",
            context=context,
            source_chars=10,
            segments=segments,
            task_id="task-1",
            window_key="WIN_0000",
        )
    )
    assert draft[0]["target_text"] == "Draft line."
    assert draft_carry["ongoing_state"] == "tension"
    first_messages = provider.calls[0]["messages"]
    assert len(first_messages) == 2
    assert "STAGE: DRAFT" in first_messages[1]["content"]
    assert "PROJECT BIBLE" in first_messages[1]["content"]

    polished, carry = __import__("asyncio").run(
        translate_window_polish(
            provider=provider,
            model="mock",
            window_id="W1",
            context=context,
            draft=draft,
            carry_over=draft_carry,
            segments=segments,
            task_id="task-1",
            window_key="WIN_0000",
        )
    )
    assert polished[0]["target_text"] == "Natural line."
    assert carry["ongoing_state"] == "resolved"
    second_messages = provider.calls[1]["messages"]
    assert second_messages[0] == first_messages[0]
    assert second_messages[1] == first_messages[1]
    assert second_messages[2]["role"] == "assistant"
    assert "Draft line." in second_messages[2]["content"]
    assert second_messages[3]["content"] == POLISH_STAGE_INSTRUCTION
    assert "你终于来了" not in second_messages[3]["content"]


def test_conversation_store_falls_back_to_one_shot_when_missing() -> None:
    provider = _conversation_provider()
    context = ContextResult(
        prompt_text="SOURCE SEGMENTS (translate only these, one translation per segment_id):\n[S1] 你好。"
    )
    segments = [{
        "segment_id": "S1",
        "segment_no": 1,
        "segment_type": "dialogue",
        "speaker_name": "林晓",
        "source_text": "你好。",
    }]
    polished, _ = __import__("asyncio").run(
        translate_window_polish(
            provider=provider,
            model="mock",
            window_id="W1",
            context=context,
            draft=[{"segment_id": "S1", "target_text": "Draft."}],
            carry_over=None,
            segments=segments,
            task_id="missing-task",
            window_key="WIN_9999",
        )
    )
    assert polished[0]["target_text"] == "Natural line."
    # 没有会话记录时仍走旧的两消息路径，不能错误复用其他窗口的上下文。
    assert len(provider.calls[0]["messages"]) == 2


def test_thinking_translation_and_qa_bind_global_standard() -> None:
    provider = FakeProvider()
    provider.handlers.append(lambda messages: {
        "window_id": "W1",
        "translations": [{
            "segment_id": "S1", "target_text": "Natural line.",
            "notes": [], "ambiguity": [],
        }],
        "carry_over": {},
    })
    context = ContextResult(
        prompt_text="SOURCE SEGMENTS (translate only these, one translation per segment_id):\n[S1] 你好。"
    )
    segments = [{
        "segment_id": "S1", "segment_no": 1, "segment_type": "dialogue",
        "speaker_name": "林晓", "source_text": "你好。",
    }]
    import asyncio

    asyncio.run(translate_window_polish(
        provider=provider, model="mock", window_id="W1", context=context,
        draft=[{"segment_id": "S1", "target_text": "Draft."}],
        carry_over=None, segments=segments,
    ))
    assert THINKING_STANDARD_MARKER in provider.calls[0]["messages"][0]["content"]

    qa = FakeProvider()
    qa.handlers.append(lambda messages: {"issues": []})
    result = asyncio.run(semantic_qa(qa, "mock", "原文", "Translated."))
    assert result.ok
    assert THINKING_STANDARD_MARKER in qa.calls[0]["messages"][0]["content"]
