"""LLM 调用层自动重试测试：瞬时故障不中断长剧本翻译。"""

import asyncio
import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.llm.openai_compat import OpenAICompatProvider
from app.llm.protocol import LLMError


class _State:
    """可编程 mock：返回序列决定每次请求的状态码/内容。"""

    def __init__(self, responses: list):
        self.responses = responses
        self.calls = 0


def _serve(state: _State):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            self.rfile.read(n)
            state.calls += 1
            idx = min(state.calls - 1, len(state.responses) - 1)
            code, content = state.responses[idx]
            data = json.dumps(content).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return H


def _run(state, max_retries=2, timeout=10.0):
    import tempfile

    from app.core.config import settings

    settings.data_dir = Path(tempfile.mkdtemp(prefix="mt_cache_test_"))
    srv = HTTPServer(("127.0.0.1", 0), _serve(state))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    provider = OpenAICompatProvider(
        base_url=f"http://127.0.0.1:{port}",
        api_key="sk-test",
        model="mock",
        max_retries=max_retries,
    )
    try:
        return asyncio.run(
            provider.chat(messages=[{"role": "user", "content": "hi"}], timeout=timeout)
        )
    finally:
        srv.shutdown()


def _ok(content="pong"):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {},
    }


def test_transient_500_retries_then_succeeds():
    """一次 500 后成功：应自动重试并返回结果。"""
    state = _State([(500, {}), (200, _ok())])
    resp = _run(state)
    assert state.calls == 2
    assert resp.content == "pong"


def test_network_refusal_retries_then_succeeds():
    """连接拒绝（瞬时网络抖动）后成功。"""
    state = _State([(503, {}), (200, _ok("after"))])
    resp = _run(state)
    assert state.calls == 2
    assert resp.content == "after"


def test_retries_exhausted_raises_retryable_error():
    """全部 5xx：耗尽 max_retries 后抛出 retryable 错误。"""
    state = _State([(500, {}), (500, {}), (500, {})])
    try:
        _run(state)
        assert False, "应抛出 LLMError"
    except LLMError as exc:
        assert exc.retryable
        assert state.calls == 3  # 1 次首调 + 2 次重试


def test_auth_error_never_retries():
    """401 不可重试：只调用一次。"""
    state = _State([(401, {})])
    try:
        _run(state)
        assert False, "应抛出 LLMError"
    except LLMError as exc:
        assert not exc.retryable
        assert exc.code == "auth"
        assert state.calls == 1


def test_bad_request_downgrades_thinking_then_succeeds():
    """400 且带 thinking：降级去掉 thinking 重试一次后成功（本地模型兼容）。"""
    state = _State([(400, {}), (200, _ok("after"))])
    resp = _run(state)
    assert state.calls == 2
    assert resp.content == "after"


def test_context_length_400_is_classified_without_retry():
    """400 正文明确为上下文超限时，必须抛 context_length 而不是 bad_request。"""
    state = _State([(
        400,
        {"error": {"message": "This model's maximum context length is 8192 tokens"}},
    )])
    try:
        _run(state)
        assert False, "应抛出 LLMError"
    except LLMError as exc:
        assert not exc.retryable
        assert exc.code == "context_length"
        assert state.calls == 1


def test_payload_too_large_413_is_context_length():
    state = _State([(413, {"error": {"message": "Request entity too large"}})])
    try:
        _run(state)
        assert False, "应抛出 LLMError"
    except LLMError as exc:
        assert not exc.retryable
        assert exc.code == "context_length"


def test_bad_request_after_downgrade_no_retry():
    """降级后仍 400（无降级空间）：不可重试。"""
    state = _State([(400, {}), (400, {}), (200, _ok())])
    try:
        _run(state)
        assert False, "应抛出 LLMError"
    except LLMError as exc:
        assert not exc.retryable
        assert exc.code == "bad_request"
        assert state.calls == 2


def test_max_retries_zero_means_no_retry():
    """max_retries=0：首调即失败不重试。"""
    state = _State([(500, {})])
    try:
        _run(state, max_retries=0)
        assert False, "应抛出 LLMError"
    except LLMError:
        assert state.calls == 1


def test_cache_hit_skips_http_call():
    """相同请求第二次命中缓存，不再发起 HTTP。"""
    import tempfile
    from pathlib import Path as _Path

    from app.core.config import settings

    settings.data_dir = _Path(tempfile.mkdtemp(prefix="mt_cache_hit_"))
    state = _State([(200, _ok("pong"))])
    srv = HTTPServer(("127.0.0.1", 0), _serve(state))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    provider = OpenAICompatProvider(
        base_url=f"http://127.0.0.1:{port}", api_key="sk-test", model="mock", max_retries=0
    )
    try:
        r1 = asyncio.run(provider.chat(messages=[{"role": "user", "content": "hi"}]))
        r2 = asyncio.run(provider.chat(messages=[{"role": "user", "content": "hi"}]))
        assert state.calls == 1  # 缓存命中，无新请求
        assert r1.content == r2.content == "pong"
    finally:
        srv.shutdown()


def test_thinking_budget_and_truncation_retry(tmp_path, monkeypatch):
    """截断根因回归：思考模式 max_tokens 给足（32K）；截断重试关思考且预算提升
    （历史做法减半到 4096 导致反复 finish_reason=length）。"""
    from app.core.config import settings

    settings.data_dir = tmp_path

    class State:
        def __init__(self):
            self.bodies: list[dict] = []
            self.calls = 0

    state = State()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(n)
            state.bodies.append(json.loads(raw))
            state.calls += 1
            if state.calls == 1:
                # 第一次：length 截断
                content = {
                    "id": "x1", "choices": [
                        {"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": '{"trunc"', "reasoning_content": "..."}}
                    ], "usage": {"prompt_tokens": 100, "completion_tokens": 8192,
                                 "completion_tokens_details": {"reasoning_tokens": 7000}}
                }
            else:
                content = {
                    "id": "x2", "choices": [
                        {"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}
                    ], "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                 "completion_tokens_details": {"reasoning_tokens": 0}}
                }
            data = json.dumps(content).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        provider = OpenAICompatProvider(
            base_url=f"http://127.0.0.1:{port}", api_key="sk-test", model="mock"
        )

        async def go():
            return await provider.chat(
                messages=[{"role": "user", "content": "translate"}],
                thinking="enabled", reasoning_effort="low",
            )

        resp = asyncio.run(go())
        assert resp.finish_reason == "stop", resp.content

        assert state.calls == 2, f"应有一次截断重试，实际 {state.calls} 次"
        first = state.bodies[0]
        second = state.bodies[1]
        # 思考模式：预算给足（思维链与译文共享 completion 预算）
        assert first["max_tokens"] == 32768, first["max_tokens"]
        assert first["thinking"] == {"type": "enabled"}
        # 截断重试：关思考 + 预算提升（不再减半）
        assert second["thinking"] == {"type": "disabled"}
        assert second["max_tokens"] >= 16384, second["max_tokens"]
        assert "concise" in second["messages"][-1]["content"].lower()
    finally:
        srv.shutdown()
