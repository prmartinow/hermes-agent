import asyncio
import logging
import time
import pytest

from hermes_cli.pty_session import (
    MemoryBudgetFull,
    PtySessionRegistry,
    RegistryFull,
)
from tests.hermes_cli.test_pty_session import FakeBridge, FakeWS


def make_reg(usage=None, *, ttl=3600.0, max_sessions=16, memory_budget=None):
    return PtySessionRegistry(
        ttl=ttl,
        max_sessions=max_sessions,
        buffer_cap=1024,
        read_timeout=0.01,
        memory_budget_bytes=memory_budget,
        memory_usage=usage,
    )


class DiagnosticsBridge(FakeBridge):
    def __init__(self, chunks=(), *, pid=None, busy=False, alive=True):
        super().__init__(chunks)
        self.pid = pid
        self._busy = busy
        self._alive = alive

    def is_alive(self):
        return self._alive and not self.closed

    def retire_if_idle(self):
        return not self._busy


@pytest.mark.asyncio
async def test_idle_eviction_reason_ttl_logged(caplog):
    caplog.set_level(logging.INFO)
    reg = make_reg(ttl=60.0)
    bridge = DiagnosticsBridge(pid=1001)
    session, _ = await reg.attach_or_spawn("secret-token-ttl-9999", spawn=lambda: bridge)
    ws = FakeWS()
    await session.attach(ws)
    reg.detach("secret-token-ttl-9999", ws)
    detached_at = session.last_detached_at

    try:
        await reg.reap_idle(now=detached_at + 120.0)
        assert bridge.closed
        assert "secret-token-ttl-9999" not in reg._sessions

        eviction_records = [r for r in caplog.records if "PTY session evicted" in r.message]
        assert len(eviction_records) == 1
        record = eviction_records[0]
        assert "reason=ttl" in record.message
        assert "pid=1001" in record.message
        assert "idle_seconds=120.0" in record.message
        assert "registry_count=0" in record.message

        # Sensitive key absent
        assert "secret-token-ttl-9999" not in caplog.text
        assert "rawkey" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_idle_eviction_reason_memory_logged(caplog):
    caplog.set_level(logging.INFO)
    usage = [0]
    reg = make_reg(usage=lambda _: usage[0], memory_budget=8000)
    bridge = DiagnosticsBridge(pid=2002)
    session, _ = await reg.attach_or_spawn("secret-token-mem-8888", spawn=lambda: bridge)
    session.detach(None)

    try:
        usage[0] = 10_000
        await reg.reap_idle()
        assert bridge.closed
        assert "secret-token-mem-8888" not in reg._sessions

        eviction_records = [r for r in caplog.records if "PTY session evicted" in r.message]
        assert len(eviction_records) == 1
        record = eviction_records[0]
        assert "reason=memory" in record.message
        assert "pid=2002" in record.message
        assert "rss_bytes=10000" in record.message
        assert "budget_bytes=8000" in record.message
        assert "registry_count=0" in record.message

        # Sensitive key absent
        assert "secret-token-mem-8888" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_idle_eviction_reason_capacity_logged(caplog):
    caplog.set_level(logging.INFO)
    reg = make_reg(max_sessions=1)
    bridge1 = DiagnosticsBridge(pid=3003)
    s1, _ = await reg.attach_or_spawn("secret-token-cap-1111", spawn=lambda: bridge1)
    s1.detach(None)

    bridge2 = DiagnosticsBridge(pid=3004)
    try:
        s2, _ = await reg.attach_or_spawn("secret-token-cap-2222", spawn=lambda: bridge2)
        assert bridge1.closed
        assert not bridge2.closed

        eviction_records = [r for r in caplog.records if "PTY session evicted" in r.message]
        assert len(eviction_records) == 1
        record = eviction_records[0]
        assert "reason=capacity" in record.message
        assert "pid=3003" in record.message
        assert "registry_count=0" in record.message

        # Neither secret token leaked
        assert "secret-token-cap-1111" not in caplog.text
        assert "secret-token-cap-2222" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_dead_session_eviction_logged(caplog):
    caplog.set_level(logging.INFO)
    reg = make_reg()
    bridge = DiagnosticsBridge(pid=4004, alive=False)
    session, _ = await reg.attach_or_spawn("secret-token-dead-5555", spawn=lambda: bridge)
    # The bridge reports dead
    try:
        await reg.reap_idle()
        assert bridge.closed

        eviction_records = [r for r in caplog.records if "PTY session evicted" in r.message]
        assert len(eviction_records) == 1
        record = eviction_records[0]
        assert "reason=dead" in record.message
        assert "pid=4004" in record.message
        assert "registry_count=0" in record.message

        # Sensitive key absent
        assert "secret-token-dead-5555" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_protected_busy_no_eviction_and_no_success_log(caplog):
    caplog.set_level(logging.INFO)
    reg = make_reg(ttl=60.0)
    bridge = DiagnosticsBridge(pid=5005, busy=True)
    session, _ = await reg.attach_or_spawn("secret-token-busy-6666", spawn=lambda: bridge)
    session.detach(None)
    detached_at = session.last_detached_at

    try:
        await reg.reap_idle(now=detached_at + 120.0)
        # Busy session must survive
        assert not bridge.closed
        assert "secret-token-busy-6666" in reg._sessions
        assert session.alive

        # No eviction log emitted
        assert "PTY session evicted" not in caplog.text
        assert "secret-token-busy-6666" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_repeated_busy_probes_do_not_duplicate_logs(caplog):
    caplog.set_level(logging.DEBUG)
    reg = make_reg(ttl=60.0)
    bridge = DiagnosticsBridge(pid=6006, busy=True)
    session, _ = await reg.attach_or_spawn("secret-busy-repeat", spawn=lambda: bridge)
    session.detach(None)
    detached_at = session.last_detached_at

    try:
        # Probe repeatedly
        for i in range(5):
            await reg.reap_idle(now=detached_at + 100.0 + i * 10)

        busy_probe_records = [r for r in caplog.records if "busy probe" in r.message and "pid=6006" in r.message]
        # Must be logged at most once, never duplicated on every tick
        assert len(busy_probe_records) == 1

        # No context or token leaked
        assert "secret-busy-repeat" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_admission_rejection_memory_unknown_distinct_in_logs(caplog):
    caplog.set_level(logging.INFO)
    # usage returns None -> memory_unknown
    reg = make_reg(usage=lambda _: None, memory_budget=8000)

    try:
        with pytest.raises(MemoryBudgetFull) as exc_info:
            await reg.attach_or_spawn("token-secret-admission-unk", spawn=lambda: DiagnosticsBridge(pid=7007))

        assert exc_info.value.rejection_reason == "memory_unknown"

        # Check logs
        rejected_records = [r for r in caplog.records if "Terminal admission rejected" in r.message]
        assert len(rejected_records) == 1
        record = rejected_records[0]
        assert "reason=memory_unknown" in record.message
        assert "budget_bytes=8000" in record.message

        assert "token-secret-admission-unk" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_admission_rejection_memory_budget_exceeded_distinct_in_logs(caplog):
    caplog.set_level(logging.INFO)
    usage = [0]
    reg = make_reg(usage=lambda _: usage[0], memory_budget=8000)

    # Put a busy terminal so it cannot be reclaimed
    busy_bridge = DiagnosticsBridge(pid=8008, busy=True)
    s1, _ = await reg.attach_or_spawn("busy-session", spawn=lambda: busy_bridge)
    s1.detach(None)

    try:
        usage[0] = 10_000
        with pytest.raises(MemoryBudgetFull) as exc_info:
            await reg.attach_or_spawn("token-secret-admission-overbudget", spawn=lambda: DiagnosticsBridge(pid=8009))

        assert exc_info.value.rejection_reason == "memory"

        # Check logs
        rejected_records = [
            r for r in caplog.records
            if "Terminal admission rejected" in r.message and "reason=memory," in r.message
        ]
        assert len(rejected_records) == 1
        record = rejected_records[0]
        assert "rss_bytes=10000" in record.message
        assert "budget_bytes=8000" in record.message
        assert "memoryunknown" not in record.message

        assert "token-secret-admission-overbudget" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_admission_rejection_capacity_in_logs(caplog):
    caplog.set_level(logging.INFO)
    reg = make_reg(max_sessions=1)
    busy_bridge = DiagnosticsBridge(pid=9009, busy=True)
    s1, _ = await reg.attach_or_spawn("busy-sess", spawn=lambda: busy_bridge)
    s1.detach(None)

    try:
        with pytest.raises(RegistryFull):
            await reg.attach_or_spawn("token-secret-capacity-fail", spawn=lambda: DiagnosticsBridge(pid=9010))

        rejected_records = [
            r for r in caplog.records
            if "Terminal admission rejected" in r.message and "reason=capacity" in r.message
        ]
        assert len(rejected_records) == 1
        record = rejected_records[0]
        assert "registry_count=1" in record.message
        assert "max_sessions=1" in record.message

        assert "token-secret-capacity-fail" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_sensitive_tokens_keys_profiles_absent_across_all_lifecycle(caplog):
    caplog.set_level(logging.DEBUG)
    usage = [0]
    reg = make_reg(ttl=10.0, max_sessions=2, memory_budget=5000, usage=lambda _: usage[0])

    secret_key = "tok_super_secret_auth_token_98765"
    secret_profile = "profile_user_alice_secret_4321"
    secret_session_id = "sessionID_corr_id_999888"

    bridge = DiagnosticsBridge(pid=9999)
    session, _ = await reg.attach_or_spawn(secret_key, spawn=lambda: bridge)
    ws = FakeWS()
    await session.attach(ws)
    reg.detach(secret_key, ws)

    try:
        # Trigger reap which evicts for memory
        usage[0] = 6000
        await reg.reap_idle()
        assert bridge.closed

        # Verify nothing sensitive in caplog text
        assert secret_key not in caplog.text
        assert secret_profile not in caplog.text
        assert secret_session_id not in caplog.text
        assert "attachtoken" not in caplog.text
        assert "profile" not in caplog.text.lower() or "profile=" not in caplog.text
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_getattr_safe_pid_when_bridge_lacks_pid(caplog):
    caplog.set_level(logging.INFO)
    reg = make_reg(ttl=10.0)
    # FakeBridge does not have pid attribute
    bridge = FakeBridge([])
    assert not hasattr(bridge, "pid")

    session, _ = await reg.attach_or_spawn("no-pid-key", spawn=lambda: bridge)
    session.detach(None)

    try:
        await reg.reap_idle(now=session.last_detached_at + 20.0)
        assert bridge.closed

        eviction_records = [r for r in caplog.records if "PTY session evicted" in r.message]
        assert len(eviction_records) == 1
        record = eviction_records[0]
        assert "reason=ttl" in record.message
        assert "pid=None" in record.message
    finally:
        await reg.close_all()
