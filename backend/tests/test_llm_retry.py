"""LLM 调用层自动重试测试：瞬时故障不中断长剧本翻译。"""

import asyncio
import json
import threading
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


def test_bad_request_no_response_format_no_retry():
    """400（无降级空间）不可重试。"""
    state = _State([(400, {}), (200, _ok())])
    try:
        _run(state)
        assert False, "应抛出 LLMError"
    except LLMError as exc:
        assert not exc.retryable
        assert state.calls == 1


def test_max_retries_zero_means_no_retry():
    """max_retries=0：首调即失败不重试。"""
    state = _State([(500, {})])
    try:
        _run(state, max_retries=0)
        assert False, "应抛出 LLMError"
    except LLMError:
        assert state.calls == 1
