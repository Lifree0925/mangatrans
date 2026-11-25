"""API Key 凭据存储。

- 开发环境：系统凭据库不可用时允许显式回退到数据目录文件。
- 正式环境（PyInstaller 冻结包或 MT_ENV=production）：只允许系统凭据库，
  不可用时抛出明确异常，禁止把 API Key 写成明文。
"""

import json
import os
import sys
from pathlib import Path

from app.core.config import settings

_SERVICE = "mangatrans"


class CredentialStoreError(RuntimeError):
    """系统凭据库不可用或凭据操作失败。"""


class CredentialDeleteError(CredentialStoreError):
    """删除凭据失败（旧 Key 可能仍然存在）。"""


def _is_production() -> bool:
    return bool(getattr(sys, "frozen", False)) or os.getenv("MT_ENV", "").strip().lower() in {
        "prod",
        "production",
    }


def _fallback_allowed() -> bool:
    if _is_production():
        return False
    return os.getenv("MT_ALLOW_FILE_CREDENTIALS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _fallback_file() -> Path:
    return settings.data_dir / "credentials.dev.json"


def _keyring():
    try:
        import keyring
    except Exception as exc:  # noqa: BLE001 缺少依赖也视为系统凭据库不可用
        raise CredentialStoreError(f"系统凭据库不可用：{exc}") from exc
    return keyring


def _keyring_get(ref: str) -> str | None:
    keyring = _keyring()
    try:
        value = keyring.get_password(_SERVICE, ref)
    except Exception as exc:  # noqa: BLE001
        raise CredentialStoreError(f"读取系统凭据库失败：{exc}") from exc
    return value or None


def _keyring_set(ref: str, secret: str) -> None:
    keyring = _keyring()
    try:
        keyring.set_password(_SERVICE, ref, secret)
        stored = keyring.get_password(_SERVICE, ref)
    except Exception as exc:  # noqa: BLE001
        raise CredentialStoreError(f"写入系统凭据库失败：{exc}") from exc
    if stored != secret:
        raise CredentialStoreError("写入系统凭据库后回读不一致")


def _read_fallback() -> dict[str, str]:
    path = _fallback_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CredentialStoreError(f"开发环境凭据文件损坏：{exc}") from exc
    return data if isinstance(data, dict) else {}


def _write_fallback(data: dict[str, str]) -> None:
    path = _fallback_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save_credential(ref: str, secret: str) -> None:
    """保存 API Key；正式环境失败时抛错，禁止明文降级。"""
    try:
        _keyring_set(ref, secret)
        return
    except CredentialStoreError:
        if not _fallback_allowed():
            raise
    data = _read_fallback()
    data[ref] = secret
    _write_fallback(data)


def get_credential(ref: str) -> str | None:
    """读取 API Key；正式环境不能静默读取/写入明文文件。"""
    if _is_production() and _fallback_file().exists():
        raise CredentialStoreError(
            "检测到未迁移的明文凭据文件，系统已拒绝继续使用；请重新保存 API Key 或完成迁移"
        )
    keyring_error: CredentialStoreError | None = None
    try:
        secret = _keyring_get(ref)
        if secret:
            return secret
    except CredentialStoreError as exc:
        keyring_error = exc

    if _fallback_allowed():
        data = _read_fallback()
        return data.get(ref)

    if keyring_error is not None:
        raise keyring_error
    return None


def delete_credential(ref: str) -> None:
    """删除凭据并回读确认；失败时抛错，调用方不得显示删除成功。"""
    keyring_error: CredentialStoreError | None = None
    keyring = None
    try:
        keyring = _keyring()
        try:
            keyring.delete_password(_SERVICE, ref)
        except Exception:
            # 有的后端对不存在的项抛 PasswordDeleteError；以删除后回读为准。
            pass
        remaining = keyring.get_password(_SERVICE, ref)
        if remaining is not None:
            raise CredentialDeleteError("系统凭据库删除后回读仍存在，旧 Key 未清除")
    except CredentialStoreError as exc:
        keyring_error = exc
    except Exception as exc:  # noqa: BLE001
        keyring_error = CredentialStoreError(f"删除系统凭据失败：{exc}")

    # 开发环境的历史回退文件也要清理，避免残留明文 Key。
    path = _fallback_file()
    fallback_touched = False
    if path.exists():
        try:
            data = _read_fallback()
            if ref in data:
                data.pop(ref, None)
                _write_fallback(data)
                fallback_touched = True
        except CredentialStoreError:
            if keyring_error is None:
                raise
    if keyring_error is not None and not (fallback_touched and _fallback_allowed()):
        raise keyring_error


def migrate_file_credentials() -> int:
    """把历史明文 Key 迁移到系统凭据库，全部回读成功后才删除明文文件。"""
    path = _fallback_file()
    if not path.exists():
        return 0
    data = _read_fallback()
    if not data:
        path.unlink(missing_ok=True)
        return 0
    for ref, secret in data.items():
        if not ref or not secret:
            continue
        _keyring_set(str(ref), str(secret))
    path.unlink(missing_ok=True)
    return len(data)
