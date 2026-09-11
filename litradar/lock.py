"""单实例锁:防止定时任务重叠运行。

实测教训:两个 enrich 同时跑会互抢 Semantic Scholar 的 1 req/s 限流,
表现为"批量全部返回空、补不到摘要",而且很难看出原因。
流水线阶段(ingest / enrich / rank / summarize)默认互斥。
"""
from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance(lock_path: str | Path, *, blocking: bool = False) -> Iterator[None]:
    """持有排他文件锁。已有实例在跑时抛 AlreadyRunning(或 blocking=True 时等待)。

    用法::

        with single_instance("data/litradar.lock"):
            ...
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                holder = ""
                try:
                    holder = os.read(fd, 64).decode("utf-8", "replace").strip()
                except OSError:
                    pass
                raise AlreadyRunning(
                    f"另一个 LitRadar 任务正在运行{f'(pid {holder})' if holder else ''};"
                    " 跳过本次以避免争抢 API 限流"
                ) from e
            raise

        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
