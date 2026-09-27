"""Memory pressure must never turn viewer absence into permission to kill work."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli.pty_session import PtySessionRegistry, RegistryFull
from tests.hermes_cli.test_pty_session import FakeBridge, FakeWS


def registry(usage=lambda sessions: 0, *, maximum=16):
    return PtySessionRegistry(ttl=3600, max_sessions=maximum, buffer_cap=1024,
                              read_timeout=.01, memory_budget_bytes=8000,
                              memory_usage=usage)


async def terminal(reg, key, *, busy=False):
    bridge = FakeBridge([])
    bridge.retire_if_idle = lambda: not busy
    session, _ = await reg.attach_or_spawn(key, spawn=lambda: bridge)
    session.detach(None)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["ttl", "capacity", "memory"])
async def test_busy_sessions_survive_all_cleanup_paths(reason):
    usage = [0]
    reg = registry(lambda _: usage[0], maximum=16 if reason == "memory" else 1)
    session = await terminal(reg, "working", busy=True)
    detached = session.last_detached_at
    try:
        if reason == "ttl":
            await reg.reap_idle(now=detached + 3601)
            assert session.last_detached_at > detached
        else:
            usage[0] = 8000 if reason == "memory" else 0
            with pytest.raises(RegistryFull):
                await terminal(reg, "new")
        assert not session.bridge.closed
        assert reg._sessions["working"] is session
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_memory_pressure_reclaims_oldest_detached_idle_only():
    pressure = [False]
    reg = registry(lambda sessions: len(sessions) * 3000 if pressure[0] else 0)
    a = await terminal(reg, "old")
    busy = await terminal(reg, "busy", busy=True)
    attached = await terminal(reg, "attached")
    await attached.attach(FakeWS())
    try:
        pressure[0] = True
        await reg.reap_idle()
        assert a.bridge.closed
        assert not busy.bridge.closed and not attached.bridge.closed
        assert set(reg._sessions) == {"busy", "attached"}
    finally:
        await reg.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [8000, 9000, None])
async def test_budget_or_unknown_blocks_new_not_existing(usage):
    measured = [0]
    reg = registry(lambda _: measured[0])
    s = await terminal(reg, "existing", busy=True)
    try:
        measured[0] = usage
        same, created = await reg.attach_or_spawn("existing", spawn=lambda: pytest.fail("respawn"))
        assert same is s and not created
        with pytest.raises(RegistryFull):
            await reg.attach_or_spawn("new", spawn=lambda: pytest.fail("overbudget spawn"))
        assert not s.bridge.closed
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_no_speculative_standby_with_budget():
    reg = registry()
    reg.configure_standby_spawn(lambda: pytest.fail("unaccounted standby"))
    await reg.ensure_standby()
    assert reg._standby_session is None


@pytest.mark.asyncio
async def test_reattach_during_retirement_cannot_be_killed_as_a_viewer():
    reg = registry(maximum=1)
    s = await terminal(reg, "old")
    entered, release = threading.Event(), threading.Event()

    def retire():
        entered.set()
        assert release.wait(5)
        return True

    s.bridge.retire_if_idle = retire
    task = asyncio.create_task(terminal(reg, "new"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert not await s.attach(FakeWS())
        reconnect = asyncio.create_task(reg.attach_or_spawn("old", spawn=lambda: FakeBridge([])))
        await asyncio.sleep(0)
        assert not reconnect.done()
        release.set()
        await task
        restored, created = await reconnect
        assert restored is not s and created and restored.alive
    finally:
        release.set()
        await reg.close_all()


@pytest.mark.asyncio
async def test_unknown_retirement_protocol_keeps_detached_process():
    reg = registry(maximum=1)
    s = await terminal(reg, "unknown")
    s.bridge.retire_if_idle = None
    try:
        await reg.reap_idle(now=time.monotonic() + 4000)
        with pytest.raises(RegistryFull):
            await terminal(reg, "new")
        assert not s.bridge.closed
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_stalled_input_does_not_bypass_working_agent_fence():
    reg = registry(maximum=1)
    s = await terminal(reg, "working", busy=True)
    s.bridge.is_alive = lambda: not s.bridge.closed
    s.bridge.write_result = False
    try:
        assert not await s.write(FakeWS(), b"input")
        await reg.reap_idle(now=time.monotonic() + 4000)
        assert not s.bridge.closed
        assert s.alive
        same, created = await reg.attach_or_spawn("working", spawn=lambda: pytest.fail("cold replacement"))
        assert same is s and not created
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_cancelled_retirement_finishes_close_before_unlocking():
    reg = registry(maximum=1)
    s = await terminal(reg, "old")
    entered, release = asyncio.Event(), asyncio.Event()
    original_close = s.close

    async def gated_close():
        entered.set()
        await release.wait()
        await original_close()

    s.close = gated_close
    task = asyncio.create_task(terminal(reg, "new"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert s.bridge.closed
        assert not reg._sessions
        assert not reg._attach_lock.locked()
    finally:
        release.set()
        await original_close()
        await reg.close_all()


def test_memory_accounting_deduplicates_and_includes_host(monkeypatch):
    from hermes_cli import pty_memory
    class Process:
        def __init__(self, pid):
            self.pid = pid
        def create_time(self):
            return self.pid * 10
        def children(self, recursive):
            return [Process(3)]
        def memory_info(self):
            return SimpleNamespace(rss=self.pid * 100)
    monkeypatch.setattr(pty_memory.os, "getpid", lambda: 1)
    monkeypatch.setattr(pty_memory.psutil, "Process", Process)
    sessions = [SimpleNamespace(bridge=SimpleNamespace(pid=2, process_birth_time=20))] * 2
    assert pty_memory.pty_memory_usage(sessions) == 600
    sessions[0].bridge.process_birth_time = 19
    assert pty_memory.pty_memory_usage(sessions) is None
