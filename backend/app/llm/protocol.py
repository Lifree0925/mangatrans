"""模型接入协议：业务层不依赖具体厂商 SDK。"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class LLMResponse:
    content: str
    provider_id: str
    model_name: str
    request_id: str
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None
    latency_ms: float = 0.0


@dataclass
class ProviderTestResult:
    ok: bool
    model_name: str
    detail: str = ""


@dataclass
class ProviderCapabilities:
    supports_json_schema: bool = False
    supports_json_object: bool = False
    supports_streaming: bool = False
    context_window: int = 8192
    max_output_tokens: int = 8192
    supports_idempotency_key: bool = False


@runtime_checkable
class LLMProvider(Protocol):
    """所有 Provider 适配器实现的统一接口。"""

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
    ) -> LLMResponse: ...

    async def test_connection(self) -> ProviderTestResult: ...


class LLMError(Exception):
    """可重试/不可重试的模型调用错误。"""

    def __init__(self, message: str, retryable: bool = False, code: str = "llm_error"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
