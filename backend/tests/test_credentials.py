"""系统凭据库安全边界：正式环境禁止明文回退，删除必须可验证。"""

import json
import sys

import pytest

import app.core.database as dbmod
from app.core import credentials
from app.core.credentials import CredentialDeleteError, CredentialStoreError
from app.models.translation import ProviderConfig


class FakeKeyring:
    def __init__(self, *, delete_noop: bool = False):
        self.store: dict[tuple[str, str], str] = {}
        self.delete_noop = delete_noop

    def set_password(self, service: str, ref: str, secret: str) -> None:
        self.store[(service, ref)] = secret

    def get_password(self, service: str, ref: str) -> str | None:
        return self.store.get((service, ref))

    def delete_password(self, service: str, ref: str) -> None:
        if not self.delete_noop:
            self.store.pop((service, ref), None)


def _isolate(monkeypatch, tmp_path, *, production: bool):
    monkeypatch.setattr(credentials.settings, "data_dir", tmp_path)
    monkeypatch.setattr(sys, "frozen", production, raising=False)
    if production:
        monkeypatch.setenv("MT_ENV", "production")
    else:
        monkeypatch.delenv("MT_ENV", raising=False)
        monkeypatch.delenv("MT_ALLOW_FILE_CREDENTIALS", raising=False)


def test_production_fails_closed_when_keyring_unavailable(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, production=True)
    monkeypatch.setattr(
        credentials, "_keyring", lambda: (_ for _ in ()).throw(CredentialStoreError("no keyring"))
    )

    with pytest.raises(CredentialStoreError):
        credentials.save_credential("ref-a", "sk-secret")
    assert not (tmp_path / "credentials.dev.json").exists()


def test_development_fallback_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, production=False)
    monkeypatch.setattr(
        credentials, "_keyring", lambda: (_ for _ in ()).throw(CredentialStoreError("no keyring"))
    )

    credentials.save_credential("ref-a", "sk-dev")
    path = tmp_path / "credentials.dev.json"
    assert path.exists()
    assert credentials.get_credential("ref-a") == "sk-dev"
    credentials.delete_credential("ref-a")
    assert credentials.get_credential("ref-a") is None


def test_delete_fails_if_key_remains(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, production=True)
    fake = FakeKeyring(delete_noop=True)
    fake.store[("mangatrans", "ref-a")] = "sk-secret"
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)

    with pytest.raises(CredentialDeleteError):
        credentials.delete_credential("ref-a")


def test_plaintext_credentials_migrate_only_after_readback(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, production=True)
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    path = tmp_path / "credentials.dev.json"
    path.write_text(json.dumps({"ref-a": "sk-secret"}), encoding="utf-8")

    assert credentials.migrate_file_credentials() == 1
    assert fake.store[("mangatrans", "ref-a")] == "sk-secret"
    assert not path.exists()


def test_production_refuses_unmigrated_plaintext_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, production=True)
    monkeypatch.setattr(credentials, "_keyring", lambda: FakeKeyring())
    (tmp_path / "credentials.dev.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CredentialStoreError):
        credentials.get_credential("ref-a")


def test_provider_delete_keeps_config_when_credential_delete_fails(client, monkeypatch):
    with dbmod.SessionLocal() as db:
        cfg = ProviderConfig(
            display_name="测试",
            base_url="http://127.0.0.1:9",
            model_name="m",
            credential_ref="ref-fail",
        )
        db.add(cfg)
        db.commit()
        pid = cfg.id

    monkeypatch.setattr(
        credentials,
        "delete_credential",
        lambda _ref: (_ for _ in ()).throw(CredentialStoreError("删除失败")),
    )
    r = client.delete(f"/api/v1/settings/providers/{pid}")
    assert r.status_code == 503
    with dbmod.SessionLocal() as db:
        assert db.get(ProviderConfig, pid) is not None
