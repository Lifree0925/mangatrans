"""后端单实例锁：同一数据目录只允许一个后端进程。

设计：
- {data_dir}/backend.lock.guard：固定 1 字节，只用于操作系统文件锁，加锁后永不写入/截断。
- {data_dir}/backend.lock：JSON 元数据（pid/port/token），供桌面壳复用既有后端。

为什么拆成两个文件：Windows msvcrt 锁与文件内容/长度绑定，若在持锁期间对同一
文件 ftruncate 再写入，锁区间可能被破坏，第二实例会绕过单实例保护。锁文件因此
必须保持固定长度且只锁不写。

POSIX 用 flock，Windows 用 msvcrt.locking；锁随进程退出自动释放。
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from app.core.config import settings


def _data_dir() -> Path:
    data_dir = Path(settings.data_dir) if settings.data_dir else Path.cwd()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _guard_file() -> Path:
    """只用于加锁的固定长度文件；持锁期间禁止写入或截断。"""
    return _data_dir() / "backend.lock.guard"


def _lock_file() -> Path:
    """后端复用元数据文件（不参与文件锁）。"""
    return _data_dir() / "backend.lock"


def _lock_handle(path: Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # Windows msvcrt.locking 从“当前文件偏移”开始锁定；必须明确从 0 锁 1 字节。
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 锁文件只锁不写：后续写端口元数据必须落到另一个 backend.lock 文件。
        return fd
    except Exception:
        os.close(fd)
        raise


def _write_metadata(data: dict) -> None:
    """原子写入复用元数据，不与文件锁共用句柄。"""
    path = _lock_file()
    text = json.dumps(data, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Windows 上目标文件偶尔被读取时可能无法 replace，退回直接覆盖。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        path.write_text(text, encoding="utf-8")


def acquire_single_instance() -> int | None:
    """获取单实例锁；成功返回保持打开的 fd（进程生命周期内不要关闭）。

    返回 None 表示无法获取（可能是已有实例存活，也可能是锁机制本身异常）。
    调用方必须再用 existing_instance_alive() 区分，不能在锁异常时直接退出。
    """
    try:
        fd = _lock_handle(_guard_file())
    except OSError:
        return None  # 已有实例持有锁（或锁文件被占用）
    _write_metadata(
        {
            "pid": os.getpid(),
            "port": settings.port,
            "token": settings.auth_token,
        }
    )
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
