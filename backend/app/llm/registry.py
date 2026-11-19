"""Provider 注册表：开发期默认 FakeProvider；Sprint 4 接入用户配置。"""

from app.llm.fake import FakeProvider
from app.llm.protocol import LLMProvider

_default: LLMProvider = FakeProvider()


def set_provider(provider: LLMProvider) -> None:
    global _default
    _default = provider


def get_provider() -> LLMProvider:
    return _default
