"""Provider 错误分类：400/413 的上下文超限必须独立于 bad_request。"""

import asyncio
import json

import pytest

from app.llm import openai_compat
from app.llm.protocol import LLMError
from app.llm.openai_compat import OpenAICompatProvider


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, status_code: int, payload: dict):
        self._response = _Response(status_code, payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self._response


def _run(monkeypatch, tmp_path, status_code: int, payload: dict) -> LLMError:
    from app.core.config import settings

    settings.data_dir = tmp_path
    monkeypatch.setattr(
        openai_compat.httpx,
        "AsyncClient",
        lambda *a, **kw: _Client(status_code, payload),
    )
    provider = OpenAICompatProvider(
        base_url="http://fake", api_key="", model="mock", max_retries=0
    )
    with pytest.raises(LLMError) as caught:
        asyncio.run(provider.chat(messages=[{"role": "user", "content": "x"}]))
    return caught.value


def test_400_context_length_is_not_bad_request(monkeypatch, tmp_path):
    err = _run(
        monkeypatch,
        tmp_path,
        400,
        {"error": {"message": "This model's maximum context length is 8192 tokens"}},
    )
    assert err.code == "context_length"
    assert err.retryable is False


def test_413_is_context_length(monkeypatch, tmp_path):
    err = _run(monkeypatch, tmp_path, 413, {"error": {"message": "too large"}})
    assert err.code == "context_length"
    assert err.retryable is False
