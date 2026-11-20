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
    """只从已完成连通性验证的 Provider 中选择默认；无可用时保持 Fake（开发环境）。"""
    cfg = db.scalar(
        select(ProviderConfig)
        .where(ProviderConfig.is_default.is_(True), ProviderConfig.is_verified.is_(True))
        .limit(1)
    )
    if cfg is None:
        cfg = db.scalar(
            select(ProviderConfig).where(ProviderConfig.is_verified.is_(True)).limit(1)
        )
    if cfg is not None:
        set_provider(build_provider(cfg))
    return get_provider()


def ensure_verified_provider(db: Session) -> None:
    """翻译任务前置检查：没有已验证模型时拒绝创建任务。"""
    verified = db.scalar(
        select(ProviderConfig).where(ProviderConfig.is_verified.is_(True)).limit(1)
    )
    if verified is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail="没有可用的模型：请先在设置中完成模型连通性测试",
        )


async def test_provider(cfg: ProviderConfig) -> dict:
    provider = build_provider(cfg)
    try:
        result = await provider.test_connection()
        return {"ok": result.ok, "model_name": result.model_name, "detail": result.detail[:200]}
    except LLMError as exc:
        return {"ok": False, "model_name": cfg.model_name, "detail": str(exc)}
