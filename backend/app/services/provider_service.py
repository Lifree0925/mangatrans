"""Provider 配置管理与 registry 联动。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import credentials
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.protocol import LLMError, LLMProvider
from app.llm.registry import get_provider, set_provider
from app.models.translation import ProviderConfig


def build_provider(cfg: ProviderConfig) -> OpenAICompatProvider:
    key = credentials.get_credential(cfg.credential_ref) or ""
    return OpenAICompatProvider(base_url=cfg.base_url, api_key=key, model=cfg.model_name)


def set_default_from_db(db: Session) -> LLMProvider:
    """按 is_default 配置替换 registry 中的 provider；无配置时保持 Fake。"""
    cfg = db.scalar(
        select(ProviderConfig).where(ProviderConfig.is_default.is_(True)).limit(1)
    )
    if cfg is not None:
        set_provider(build_provider(cfg))
    return get_provider()


async def test_provider(cfg: ProviderConfig) -> dict:
    provider = build_provider(cfg)
    try:
        result = await provider.test_connection()
        return {"ok": result.ok, "model_name": result.model_name, "detail": result.detail[:200]}
    except LLMError as exc:
        return {"ok": False, "model_name": cfg.model_name, "detail": str(exc)}
