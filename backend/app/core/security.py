"""本地 API 安全：会话令牌校验。"""

import secrets

_TOKEN: str | None = None


def generate_token() -> str:
    """每次启动生成高强度随机会话令牌。"""
    return secrets.token_urlsafe(32)


def set_token(token: str) -> None:
    global _TOKEN
    _TOKEN = token


def check_token(token: str | None) -> bool:
    """常量时间比较，防时序攻击。"""
    return bool(_TOKEN and token and secrets.compare_digest(_TOKEN, token))
