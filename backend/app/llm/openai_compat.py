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
        thinking: str | None = "disabled",
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        model = model or self.model
        # 输出预算：思考模式下 reasoning 与 content 共享 completion 预算（DeepSeek 官方语义），
        # 显式传 8192 会导致思维链挤占译文空间、finish_reason=length（历史截断根因）。
        # 思考模式给 32K（官方默认 64K 的收敛值），非思考按调用方/能力值。
        if max_tokens is None:
            # thinking 为 None 时服务商默认开启思考模式，同样需要大预算
            max_tokens = (
                32768 if thinking != "disabled" else self.capabilities.max_output_tokens
            )
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature if temperature is not None else 0.3,
            # 显式限制输出预算：依赖供应商默认值曾导致长段翻译被静默截断（漏译后半情节）；
            # 截断降级重试时可用 max_tokens 覆盖更合适的预算
            "max_tokens": max(1, int(max_tokens)),
        }
        # 思考模式显式控制：默认关闭（只有润色/质检环节显式开启），避免依赖服务商默认行为
        if thinking is not None:
            body["thinking"] = {"type": thinking}
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
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
        _truncation_retried = False
        cache_key = ""
        if temperature is None or temperature <= 0.3:
            cache_key = hashlib.sha256(
                json.dumps(
                    [self.base_url, model, messages, body.get("response_format"),
                     thinking, reasoning_effort, body.get("max_tokens")],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            cached = self._cache.get(cache_key)
            if cached is not None and cached.get("finish_reason") != "length":
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
                    # 本地模型（Ollama/vLLM 等）可能不支持 thinking / response_format：
                    # 逐级降级后在同一连接内重试一次（不能移出 with 块，client 已关闭）
                    if r.status_code == 400:
                        if "thinking" in body:
                            body.pop("thinking", None)
                            body.pop("reasoning_effort", None)
                            r = await client.post(
                                f"{self.base_url}/chat/completions", json=body, headers=headers
                            )
                        elif "response_format" in body:
                            body.pop("response_format", None)
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
        # 输出截断防护：思考型模型可能耗尽输出预算返回空内容。
        # 追加"简洁输出"指令重试一次（所有调用点受益，无需各服务自行处理）。
        if choice.get("finish_reason") == "length" and not _truncation_retried:
            _truncation_retried = True
            retry_messages = list(messages) + [
                {
                    "role": "system",
                    "content": (
                        "Your previous output hit the length limit and was truncated. "
                        "Output ONLY the required JSON now. Be extremely concise: skip all reasoning, explanation and commentary."
                    ),
                }
            ]
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    retry_body = dict(body)
                    retry_body["messages"] = retry_messages
                    # 截断重试降级：关闭思考后 reasoning 不再占预算，max_tokens 提升回
                    # 译文专属空间（不再减半——历史做法把预算砍到 4096 导致反复截断）
                    retry_body.pop("reasoning_effort", None)
                    retry_body["thinking"] = {"type": "disabled"}
                    retry_body["max_tokens"] = max(
                        int(retry_body.get("max_tokens", 8192)), 16384
                    )
                    r2 = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=retry_body,
                        headers=headers,
                    )
                if r2.status_code == 200:
                    data2 = r2.json()
                    choice = data2.get("choices", [{}])[0]
                    data = data2
            except Exception:  # noqa: BLE001 重试失败按原响应处理
                pass
        message = choice.get("message", {})
        content = message.get("content") or ""
        if cache_key and choice.get("finish_reason") != "length":
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
