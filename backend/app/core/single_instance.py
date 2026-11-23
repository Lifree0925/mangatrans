"""后端单实例锁：同一数据目录只允许一个后端进程。

锁文件 {data_dir}/backend.lock：持锁进程写入 {pid, port, token} JSON，
供桌面壳复用已有后端（App 崩溃后孤儿进程不再堆积，也不会并发写库）。

POSIX 用 flock，Windows 用 msvcrt.locking；锁随进程退出自动释放。
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from app.core.config import settings


def _lock_handle(path: Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # Windows msvcrt 锁定需要文件至少 1 字节；空文件先占位
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        # 关键：msvcrt.locking 从「当前文件偏移」开始锁定 1 字节。
        # 空文件写入占位后偏移为 1，若不 seek 回 0，Windows 上锁定会失败，
        # 导致后端被误判为「已有实例」而直接退出（新装用户无法启动的根因）。
        os.lseek(fd, 0, os.SEEK_SET)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except Exception:
        os.close(fd)
        raise


def _lock_file() -> Path:
    data_dir = Path(settings.data_dir) if settings.data_dir else Path.cwd()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "backend.lock"


def acquire_single_instance() -> int | None:
    """获取单实例锁；成功返回保持打开的 fd（进程生命周期内不要关闭）。

    返回 None 表示无法获取（可能是已有实例存活，也可能是锁机制本身异常）。
    调用方必须再用 existing_instance_alive() 区分，不能在锁异常时直接退出，
    否则新装用户会因锁文件实现问题导致后端不启动、前端永久连接失败。
    """
    path = _lock_file()
    try:
        fd = _lock_handle(path)
    except OSError:
        return None  # 已有实例持有锁（或文件被占用）
    # 覆盖写入本实例信息，供下次启动复用探测。
    # ftruncate 不改变文件偏移，必须先 seek 到 0，否则 JSON 前会残留占位字节导致解析失败。
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(
        fd,
        json.dumps(
            {
                "pid": os.getpid(),
                "port": settings.port,
                "token": settings.auth_token,
            }
        ).encode(),
    )
    os.fsync(fd)
    return fd


def existing_instance_alive(timeout: float = 1.5) -> bool:
    """探测 lock 文件中记录的已有后端是否健康（仅锁获取失败时用于区分场景）。"""
    try:
        data = json.loads(_lock_file().read_text(encoding="utf-8"))
        port = int(data.get("port") or 0)
        if not port:
            return False
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 lock 文件缺失/损坏/进程已死均视为无实例
        return False
