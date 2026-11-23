"""单实例锁回归：空锁文件必须从偏移 0 加锁（Windows 新装启动失败根因）。"""

import os

from app.core.config import settings
from app.core.single_instance import _lock_handle, existing_instance_alive


def test_lock_handle_seeks_to_zero_on_empty_file(tmp_path):
    path = tmp_path / "backend.lock"
    fd = _lock_handle(path)
    try:
        # 空文件写入占位后必须回到 0；Windows msvcrt.locking 从当前偏移锁定，
        # 偏移 1 会导致新装用户后端启动失败（历史根因）
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert os.fstat(fd).st_size == 1
    finally:
        os.close(fd)


def test_existing_instance_alive_false_without_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert existing_instance_alive() is False


def test_existing_instance_alive_false_with_corrupt_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "backend.lock").write_text("0", encoding="utf-8")
    assert existing_instance_alive() is False
