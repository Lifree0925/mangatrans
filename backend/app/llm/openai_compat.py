"""OpenAI 兼容 Provider：任意 Base URL + Key + Model + 自动重试。"""

import asyncio
import hashlib
import json
import threading
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


class _LLMCache:
    """进程级 + 文件持久化的 LLM 响应缓存（重复/重试调用零成本复用）。

    仅缓存确定输出（temperature<=0.3 的翻译调用）；容量上限防膨胀。
    """

    MAX_ENTRIES = 800

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        try:
            import os

            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    self._data = json.load(f)
        except Exception:  # noqa: BLE001 缓存损坏不影响功能
            self._data = {}

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            if len(self._data) >= self.MAX_ENTRIES:
                for k in list(self._data)[: self.MAX_ENTRIES // 2]:
                    self._data.pop(k, None)
            self._data[key] = value
            if self._path:
                try:
                    import os

                    os.makedirs(os.path.dirname(self._path), exist_ok=True)
                    with open(self._path, "w", encoding="utf-8") as f:
                        json.dump(self._data, f, ensure_ascii=False)
                except Exception:  # noqa: BLE001 写失败忽略
                    pass


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.capabilities = capabilities or ProviderCapabilities()
        from app.core.config import settings

        cache_path = str(settings.data_dir / "cache" / "llm_cache.json")
        self._cache = _LLMCache(cache_path)
        self.max_retries = max(0, int(max_retries))

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
        headers = {"Content-Type": "application/json"}
        # 本地模型（Ollama / vLLM 等）通常无需 API Key
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # 确定性调用缓存：temperature<=0.3 时命中直接返回（重试/重跑零成本）
        cache_key = ""
        if temperature is None or temperature <= 0.3:
            cache_key = hashlib.sha256(
                json.dumps(
                    [self.base_url, model, messages, body.get("response_format")],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            cached = self._cache.get(cache_key)
            if cached is not None:
                return LLMResponse(
                    content=cached.get("content", ""),
                    provider_id="openai_compat",
                    model_name=model,
                    request_id=cached.get("request_id") or uuid.uuid4().hex,
                    usage=cached.get("usage", {}),
                    finish_reason=cached.get("finish_reason"),
                    latency_ms=0.0,
                )

        # 自动重试：网络抖动/超时/限流/5xx 按 max_retries 指数退避。
        # 长剧本约 60 个窗口，任一窗口一次瞬时故障都不应中断整本翻译。
        t0 = time.perf_counter()
        last_err: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            last_err = None
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(
                        f"{self.base_url}/chat/completions", json=body, headers=headers
                    )
            except httpx.TimeoutException as exc:
                last_err = LLMError("请求超时", retryable=True, code="timeout")
            except httpx.HTTPError as exc:
                last_err = LLMError(f"网络错误: {exc}", retryable=True, code="network")

            if last_err is not None:
                if attempt >= self.max_retries:
                    raise last_err
                await asyncio.sleep(2 * (2**attempt))
                continue

            # 本地模型可能不支持 response_format：降级为纯 Prompt 约束后重试一次
            if r.status_code == 400 and "response_format" in body:
                body.pop("response_format", None)
                r = await client.post(
                    f"{self.base_url}/chat/completions", json=body, headers=headers
                )

            if r.status_code in (401, 403):
                raise LLMError("认证失败", retryable=False, code="auth")
            if r.status_code == 404:
                raise LLMError("模型不存在", retryable=False, code="model_not_found")
            if r.status_code == 400:
                # 请求本身有误（含降级后仍被拒）：重试无意义
                raise LLMError(f"请求被拒绝 {r.status_code}", retryable=False, code="bad_request")
            if r.status_code == 429:
                last_err = LLMError("限流", retryable=True, code="rate_limit")
            elif r.status_code >= 500:
                last_err = LLMError(f"服务端错误 {r.status_code}", retryable=True, code="server")
            else:
                last_err = None

            if last_err is None:
                break
            if attempt >= self.max_retries:
                raise last_err
            await asyncio.sleep(2 * (2**attempt))

        latency = (time.perf_counter() - t0) * 1000
        try:
            data = r.json()
        except ValueError as exc:
            # 网关/代理可能返回非 JSON 错误页：按瞬时故障重试语义抛出
            raise LLMError(
                f"响应不是有效 JSON: {exc}", retryable=True, code="bad_json"
            ) from exc
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        if cache_key:
            self._cache.put(
                cache_key,
                {
                    "content": content,
                    "request_id": data.get("id") or request_id or uuid.uuid4().hex,
                    "usage": data.get("usage") or {},
                    "finish_reason": choice.get("finish_reason"),
                },
            )
        return LLMResponse(
            content=content,
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
