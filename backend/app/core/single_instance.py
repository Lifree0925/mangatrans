"""后端单实例锁：同一数据目录只允许一个后端进程。

锁文件 {data_dir}/backend.lock：持锁进程写入 {pid, port, token} JSON，
供桌面壳复用已有后端（App 崩溃后孤儿进程不再堆积，也不会并发写库）。

POSIX 用 flock，Windows 用 msvcrt.locking；锁随进程退出自动释放。
"""

import json
import os
import sys
from pathlib import Path

from app.core.config import settings


def _lock_handle(path: Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    # Windows msvcrt 锁定需要文件至少 1 字节；空文件先占位
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"0")
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _lock_file() -> Path:
    data_dir = Path(settings.data_dir) if settings.data_dir else Path.cwd()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "backend.lock"


def acquire_single_instance() -> int | None:
    """获取单实例锁；成功返回保持打开的 fd（进程生命周期内不要关闭）。

    已有实例存活时返回 None（调用方应退出）；锁信息同步写入 lock 文件。
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
