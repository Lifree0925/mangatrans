"""FakeProvider：测试与离线开发用，脚本化返回。"""

import time
import uuid
import json

from app.llm.protocol import (
    LLMProvider,
    LLMResponse,
    ProviderCapabilities,
    ProviderTestResult,
)


class FakeProvider:
    """按注册的 handler 脚本返回内容；未注册时返回空 JSON 对象。"""

    capabilities = ProviderCapabilities(supports_json_object=True, context_window=128_000)

    def __init__(self):
        self.handlers: list = []  # 每个元素为 callable(messages) -> dict | None
        self.calls: list[dict] = []

    def on(self, handler):
        """注册一个脚本处理器：收到消息返回 dict，返回 None 则落到默认。"""
        self.handlers.append(handler)
        return self

    def default_response(self, messages: list[dict]) -> dict:
        return {}

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        response_schema: dict | None = None,
        temperature: float | None = None,
        timeout: float = 120,
        request_id: str | None = None,
        thinking: str | None = "disabled",
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        stage: str = "llm",
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "schema": response_schema, "thinking": thinking})
        result = None
        for h in self.handlers:
            r = h(messages)
            if r is not None:
                result = r
                break
        if result is None:
            result = self.default_response(messages)
        return LLMResponse(
            content=json.dumps(result, ensure_ascii=False),
            provider_id="fake",
            model_name=model or "fake-model",
            request_id=request_id or uuid.uuid4().hex,
            usage={"total_tokens": 10},
            finish_reason="stop",
            latency_ms=1.0,
        )

    async def test_connection(self) -> ProviderTestResult:
        return ProviderTestResult(ok=True, model_name="fake-model")
