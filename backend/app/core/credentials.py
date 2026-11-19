"""API Key 凭据存储：优先系统凭据库（Windows Credential Manager / macOS Keychain），
开发环境 fallback 到 data_dir 下文件（仅开发用，正式发布不启用 fallback）。"""

import json
from pathlib import Path

from app.core.config import settings

_SERVICE = "mangatrans"
_FALLBACK_ENABLED = True  # 开发环境启用；Windows 正式构建走 keyring


def _fallback_file() -> Path:
    return settings.data_dir / "credentials.dev.json"


def save_credential(ref: str, secret: str) -> None:
    try:
        import keyring  # 延迟导入

        keyring.set_password(_SERVICE, ref, secret)
        return
    except Exception:  # noqa: BLE001 开发环境无系统凭据库
        if _FALLBACK_ENABLED:
            path = _fallback_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            data[ref] = secret
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return
        raise


def get_credential(ref: str) -> str | None:
    try:
        import keyring

        secret = keyring.get_password(_SERVICE, ref)
        if secret:
            return secret
    except Exception:  # noqa: BLE001
        pass
    path = _fallback_file()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(ref)
    return None


def delete_credential(ref: str) -> None:
    try:
        import keyring

        keyring.delete_password(_SERVICE, ref)
    except Exception:  # noqa: BLE001
        pass
    path = _fallback_file()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop(ref, None)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
