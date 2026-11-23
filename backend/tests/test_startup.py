"""首次启动回归：源码运行时必须定位到 backend/migrations，而不是仓库根目录。"""

import sys
from pathlib import Path

from app.core.config import settings
from app.main import run_migrations_if_needed


def test_migration_base_falls_back_to_backend_root(tmp_path, monkeypatch):
    captured: dict[str, str] = {}

    def fake_upgrade(cfg, revision):
        captured["script_location"] = cfg.get_main_option("script_location")
        captured["revision"] = revision

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "migrate_on_start", "1")
    monkeypatch.setattr("alembic.command.upgrade", fake_upgrade)

    run_migrations_if_needed()

    backend_root = Path(__file__).resolve().parents[1]
    assert Path(captured["script_location"]) == backend_root / "migrations"
    assert captured["revision"] == "head"
