"""_raise_fd_limit + _communicate_or_kill — descriptor-budget hardening.

The daemon fans out one asyncio network poller per provider plus a uvicorn
server. Under the macOS default soft limit of 256 a transient burst (sleep/
wake socket churn, dashboard reconnects) can exhaust descriptors, surfacing
as "OSError: too many open files" on several panels at once. These guard the
two fixes: raising our own fd limit, and not leaking pipes on subprocess
timeout.
"""
import asyncio
import resource

import pytest

from daemon import _communicate_or_kill, _raise_fd_limit


def test_raise_fd_limit_bumps_low_soft_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    low = 256 if hard == resource.RLIM_INFINITY else min(256, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (low, hard))
        old, new = _raise_fd_limit(target=4096)
        assert old == low
        want = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
        assert new >= want
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_raise_fd_limit_never_lowers():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    cur = 8192 if hard == resource.RLIM_INFINITY else min(8192, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (cur, hard))
        old, new = _raise_fd_limit(target=1024)  # below current soft
        assert old == cur
        assert new == cur  # never lowered
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


class _HangingProc:
    """Fake subprocess whose communicate() never returns in time."""

    def __init__(self) -> None:
        self.killed = False
        self.waited = False

    async def communicate(self):
        await asyncio.sleep(10)
        return b"", b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self):
        self.waited = True
        return -9


def test_communicate_or_kill_reaps_child_on_timeout():
    proc = _HangingProc()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_communicate_or_kill(proc, 0.01))
    assert proc.killed, "child must be killed on timeout so pipe fds are released"
    assert proc.waited, "child must be reaped so it doesn't linger as a zombie"


class _FastProc:
    async def communicate(self):
        return b"out", b"err"

    def kill(self) -> None:  # pragma: no cover - should not be called
        raise AssertionError("kill() must not run when communicate() succeeds")

    async def wait(self):  # pragma: no cover
        return 0


def test_communicate_or_kill_passes_through_on_success():
    out, err = asyncio.run(_communicate_or_kill(_FastProc(), 5))
    assert out == b"out"
    assert err == b"err"
