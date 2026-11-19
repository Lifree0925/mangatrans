"""OpenAI 兼容 Provider：任意 Base URL + Key + Model。"""

import json
import time
import uuid

import httpx

from app.llm.protocol import (
    LLMError,
    LLMProvider,
    LLMResponse,
    ProviderCapabilities,
    ProviderTestResult,
)


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        capabilities: ProviderCapabilities | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.capabilities = capabilities or ProviderCapabilities()

    async def chat(
        self,
        *,
        model: str | None = None,
        messages: list[dict],
        response_schema: dict | None = None,
        temperature: float | None = None,
        timeout: float = 120,
        request_id: str | None = None,
    ) -> LLMResponse:
        model = model or self.model
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature if temperature is not None else 0.3,
        }
        if response_schema is not None:
            if self.capabilities.supports_json_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": response_schema},
                }
            else:
                body["response_format"] = {"type": "json_object"}
                body["messages"] = messages + [
                    {
                        "role": "system",
                        "content": "Return ONLY a JSON object matching this schema:\n"
                        + json.dumps(response_schema, ensure_ascii=False),
                    }
                ]
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(
                    f"{self.base_url}/chat/completions", json=body, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise LLMError("请求超时", retryable=True, code="timeout") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"网络错误: {exc}", retryable=True, code="network") from exc

        latency = (time.perf_counter() - t0) * 1000
        if r.status_code in (401, 403):
            raise LLMError("认证失败", retryable=False, code="auth")
        if r.status_code == 404:
            raise LLMError("模型不存在", retryable=False, code="model_not_found")
        if r.status_code == 429:
            raise LLMError("限流", retryable=True, code="rate_limit")
        if r.status_code >= 400:
            raise LLMError(f"服务端错误 {r.status_code}", retryable=True, code="server")

        data = r.json()
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        return LLMResponse(
            content=message.get("content") or "",
            provider_id="openai_compat",
            model_name=model,
            request_id=data.get("id") or request_id or uuid.uuid4().hex,
            usage=data.get("usage") or {},
            finish_reason=choice.get("finish_reason"),
            latency_ms=latency,
        )

    async def test_connection(self) -> ProviderTestResult:
        try:
            resp = await self.chat(
                messages=[{"role": "user", "content": "ping"}], timeout=15
            )
            return ProviderTestResult(ok=True, model_name=self.model, detail=resp.content[:100])
        except LLMError as exc:
            return ProviderTestResult(ok=False, model_name=self.model, detail=str(exc))
