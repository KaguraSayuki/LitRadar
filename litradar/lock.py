"""单实例锁:防止定时任务重叠运行。

实测教训:两个 enrich 同时跑会互抢 Semantic Scholar 的 1 req/s 限流,
表现为"批量全部返回空、补不到摘要",而且很难看出原因。
流水线阶段(ingest / enrich / rank / summarize)默认互斥。

平台分流:POSIX 用 ``fcntl.flock``(整文件建议锁),Windows 用
``msvcrt.locking``(强制字节范围锁)。两者都由内核在进程退出时自动释放,
所以崩溃不会留下死锁 —— 这也是不用"文件存在即视为锁定"的原因:那种做法
在进程被 kill 之后需要人工清理。
"""
from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if os.name == "nt":  # pragma: no cover - 平台在导入期决定
    import msvcrt
else:
    import fcntl

# 平台在导入期定一次。运行期反复看 os.name 既没必要,也让"模拟另一个平台"
# 变得不可能(os.name 一改,pathlib 会立刻换成 WindowsPath 而无法在本机使用)。
_IS_WINDOWS = os.name == "nt"


class AlreadyRunning(RuntimeError):
    pass


# Windows 的字节范围锁是强制的:被别人锁住的字节连读都会失败。因此锁打在
# 文件中部,pid 留在开头(前 32 字节),持锁进程才能在"另一个任务正在运行"
# 的提示里读出对方的 pid。
_PID_BYTES = 32
_WINDOWS_LOCK_OFFSET = 64
# msvcrt.locking 在锁冲突时给出的 errno/winerror(不同 Windows 版本不一致)。
_HELD_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK,
                getattr(errno, "EDEADLOCK", errno.EDEADLK)}
_HELD_WINERRORS = {33, 36}          # ERROR_LOCK_VIOLATION / _SHARING_BUFFER_EXCEEDED


def _is_held(error: OSError) -> bool:
    """判断 OSError 是"锁已被占用"而不是真正的 I/O 故障。"""
    if error.errno in _HELD_ERRNOS:
        return True
    return getattr(error, "winerror", None) in _HELD_WINERRORS


def _acquire(fd: int, *, blocking: bool) -> None:
    if not _IS_WINDOWS:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        return
    # msvcrt.locking 从当前位置开始锁 nbytes;LK_LOCK 只重试 10 次(每次 1 秒)
    # 就放弃,不能当真正的阻塞用,所以阻塞模式自己轮询。
    os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
    if not blocking:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    while True:
        try:
            os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if not _is_held(error):
                raise
            time.sleep(0.2)


def _release(fd: int) -> None:
    if not _IS_WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _prepare(fd: int) -> None:
    """Windows:保证被锁的字节存在。锁到 EOF 之外是允许的,预置更稳妥。"""
    if not _IS_WINDOWS:
        return
    if os.fstat(fd).st_size <= _WINDOWS_LOCK_OFFSET:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        os.write(fd, b"\0")
        os.fsync(fd)


def _write_holder(fd: int) -> None:
    """把当前 pid 写进文件,供后来者报出"谁在跑"。"""
    payload = str(os.getpid()).encode("ascii")
    if _IS_WINDOWS:
        # 不能截断:被锁的范围在文件中段,缩短文件可能让已有锁出错。所以
        # 定长覆盖写,靠右补空格保证上一次的长 pid 不会残留数字。
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload.ljust(_PID_BYTES - 1, b" ") + b"\n")
    else:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
    os.fsync(fd)


def _read_holder(fd: int) -> str:
    """尽力读出持锁进程的 pid;读不到就返回空串(不影响锁语义)。"""
    try:
        raw = os.pread(fd, _PID_BYTES, 0)
    except OSError:
        return ""
    return raw.decode("utf-8", "replace").replace("\x00", " ").strip()


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
        _prepare(fd)
        try:
            _acquire(fd, blocking=blocking)
        except OSError as e:
            if _is_held(e):
                holder = _read_holder(fd)
                raise AlreadyRunning(
                    f"另一个 LitRadar 任务正在运行{f'(pid {holder})' if holder else ''};"
                    " 跳过本次以避免争抢 API 限流"
                ) from e
            raise

        _write_holder(fd)
        yield
    finally:
        try:
            _release(fd)
        except OSError:
            pass
        os.close(fd)
