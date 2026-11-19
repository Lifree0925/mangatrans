"""LLM 接入层：协议、Fake 与 OpenAI 兼容实现。"""

from app.llm.fake import FakeProvider
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.protocol import LLMError, LLMProvider, LLMResponse

__all__ = ["FakeProvider", "OpenAICompatProvider", "LLMError", "LLMProvider", "LLMResponse"]
