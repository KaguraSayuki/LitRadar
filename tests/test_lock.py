"""单实例锁:POSIX 真实行为 + 模拟 Windows(msvcrt)分支。

Windows 分支无法在 Linux 上真跑,所以这里用假的 ``msvcrt`` 模块 + 把
``os.name`` 临时改成 ``nt`` 后 reload 的方式,把真实的平台分支代码走一遍:
断言锁偏移、锁定/解锁调用、pid 落盘与读取、以及冲突时的 AlreadyRunning
和阻塞重试语义。
"""
from __future__ import annotations

import contextlib
import errno
import importlib
import os
import sys
import threading
import time
import types

import pytest

from litradar import lock


# --------------------------------------------------------------- POSIX 真实路径
def test_lock_is_exclusive_and_reports_holder(tmp_path):
    path = tmp_path / "litradar.lock"

    with lock.single_instance(path):
        assert path.exists()
        with pytest.raises(lock.AlreadyRunning) as caught:
            with lock.single_instance(path):
                pass
        # 提示里应带上持锁进程的 pid
        assert str(os.getpid()) in str(caught.value)

    # 退出 with 后必须能重新获取(进程崩溃也不该留下死锁)
    with lock.single_instance(path):
        pass


def test_lock_file_parent_is_created(tmp_path):
    path = tmp_path / "nested" / "deeper" / "litradar.lock"
    with lock.single_instance(path):
        assert path.exists()


def test_uncontended_blocking_acquire_works(tmp_path):
    with lock.single_instance(tmp_path / "litradar.lock", blocking=True):
        pass


def test_separate_files_do_not_conflict(tmp_path):
    with lock.single_instance(tmp_path / "a.lock"):
        with lock.single_instance(tmp_path / "b.lock"):
            pass


# ------------------------------------------------- 模拟 Windows(msvcrt)分支
class FakeMsvcrt:
    """记录 locking 调用;可模拟"锁被别的进程占着"。"""

    LK_NBLCK = 2
    LK_UNLCK = 0
    LK_LOCK = 1

    def __init__(self, *, held_times: int = 0):
        self.held_times = held_times
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        self.calls.append((mode, nbytes, offset))
        if mode != self.LK_UNLCK and self.held_times > 0:
            self.held_times -= 1
            raise OSError(errno.EACCES, "Permission denied")


@contextlib.contextmanager
def simulated_windows(monkeypatch, fake: FakeMsvcrt):
    """在 os.name == 'nt' + 假 msvcrt 下,用全新命名空间加载一份 lock 模块。

    不能用 importlib.reload:它在同一个模块字典上重跑代码,先前的 POSIX 导入
    留下的 ``fcntl`` 名字不会消失,Windows 分支就测不干净了。
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    spec = importlib.util.spec_from_file_location("litradar_lock_windows_sim",
                                                  lock.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 加载完立刻复原:平台分支已固化在模块内的 _IS_WINDOWS 上,而 os.name
    # 停在 "nt" 会让 pathlib 换成 WindowsPath,在本机根本实例化不了。
    monkeypatch.undo()
    assert not hasattr(module, "fcntl"), "Windows 分支不该导入 fcntl"
    yield module


def test_windows_lock_uses_msvcrt_at_a_pid_safe_offset(tmp_path, monkeypatch):
    fake = FakeMsvcrt()
    path = tmp_path / "litradar.lock"

    with simulated_windows(monkeypatch, fake) as win:
        with win.single_instance(path):
            # 锁必须避开 pid 所在的头部,否则持锁者读不到 pid(Windows 是强制锁)
            assert fake.calls[0][0] == fake.LK_NBLCK
            assert fake.calls[0][1] == 1
            assert fake.calls[0][2] == win._WINDOWS_LOCK_OFFSET
            assert win._WINDOWS_LOCK_OFFSET >= win._PID_BYTES

            fd = os.open(path, os.O_RDONLY)
            try:
                assert win._read_holder(fd) == str(os.getpid())
            finally:
                os.close(fd)
        assert fake.calls[-1][0] == fake.LK_UNLCK
        assert fake.calls[-1][2] == win._WINDOWS_LOCK_OFFSET


def test_windows_lock_conflict_raises_already_running(tmp_path, monkeypatch):
    fake = FakeMsvcrt(held_times=1)
    with simulated_windows(monkeypatch, fake) as win:
        with pytest.raises(win.AlreadyRunning, match="正在运行"):
            with win.single_instance(tmp_path / "litradar.lock"):
                pass


def test_windows_lock_prepares_the_locked_byte(tmp_path, monkeypatch):
    """锁到 EOF 之外虽然允许,但先把字节写出来更稳妥。"""
    fake = FakeMsvcrt()
    path = tmp_path / "litradar.lock"
    with simulated_windows(monkeypatch, fake) as win:
        with win.single_instance(path):
            assert path.stat().st_size > win._WINDOWS_LOCK_OFFSET


def test_windows_blocking_retries_until_it_gets_the_lock(tmp_path, monkeypatch):
    fake = FakeMsvcrt(held_times=3)
    slept: list[float] = []
    path = tmp_path / "litradar.lock"

    with simulated_windows(monkeypatch, fake) as win:
        monkeypatch.setattr(win.time, "sleep", slept.append)
        with win.single_instance(path, blocking=True):
            pass

    # 前 3 次冲突后重试成功,解锁一次
    assert [c[0] for c in fake.calls] == [fake.LK_NBLCK] * 4 + [fake.LK_UNLCK]
    assert len(slept) == 3


def test_pid_padding_does_not_leave_stale_digits(tmp_path, monkeypatch):
    fake = FakeMsvcrt()
    path = tmp_path / "litradar.lock"
    with simulated_windows(monkeypatch, fake) as win:
        # 先塞一个更长的假 pid,再正常写一次,读数不该掺进残留数字
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            os.write(fd, b"99999999")
            win._write_holder(fd)
            assert win._read_holder(fd) == str(os.getpid())
        finally:
            os.close(fd)


def test_read_holder_is_best_effort(tmp_path):
    """读不到 pid(权限/平台差异)只能让提示少一段,不能影响锁语义。"""
    path = tmp_path / "litradar.lock"
    path.write_text("", encoding="utf-8")
    fd = os.open(path, os.O_RDWR)
    try:
        assert lock._read_holder(fd) == ""
    finally:
        os.close(fd)


# ------------------------------------------------------------ 并发(真实线程)
def test_nonblocking_conflict_between_threads(tmp_path):
    path = tmp_path / "litradar.lock"
    acquired = threading.Event()
    finish = threading.Event()

    def holder():
        with lock.single_instance(path):
            acquired.set()
            finish.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert acquired.wait(5)
        with pytest.raises(lock.AlreadyRunning):
            with lock.single_instance(path):
                pass
    finally:
        finish.set()
        thread.join(5)
        time.sleep(0.05)

    with lock.single_instance(path):
        pass
