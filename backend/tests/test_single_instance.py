"""单实例锁回归：空锁文件必须从偏移 0 加锁（Windows 新装启动失败根因）。"""

import os

from app.core.config import settings
import json

from app.core.single_instance import (
    _lock_handle,
    acquire_single_instance,
    existing_instance_alive,
)


def test_lock_guard_stays_fixed_length(tmp_path):
    path = tmp_path / "backend.lock.guard"
    fd = _lock_handle(path)
    try:
        # 空文件写入占位后必须回到 0；锁后文件保持固定 1 字节，不能被截断。
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert os.fstat(fd).st_size == 1
    finally:
        os.close(fd)
    assert path.read_bytes() == b"0"


def test_metadata_written_to_separate_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "port", 39001)
    monkeypatch.setattr(settings, "auth_token", "test-token")
    fd = acquire_single_instance()
    assert fd is not None
    try:
        # 锁始终在 guard 文件上，元数据文件独立存在，持锁期间不截断 guard。
        assert (tmp_path / "backend.lock.guard").read_bytes() == b"0"
        meta = json.loads((tmp_path / "backend.lock").read_text(encoding="utf-8"))
        assert meta["port"] == 39001
        assert meta["token"] == "test-token"
    finally:
        os.close(fd)


def test_existing_instance_alive_false_without_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert existing_instance_alive() is False


def test_existing_instance_alive_false_with_corrupt_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "backend.lock").write_text("0", encoding="utf-8")
    assert existing_instance_alive() is False
