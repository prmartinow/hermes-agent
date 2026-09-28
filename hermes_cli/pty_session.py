"""Keep-alive PTY sessions for dashboard terminals.

A PTY process outlives the WebSocket that created it: a single drain task always reads the PTY into
a bounded RingBuffer and forwards to the attached socket when present. Reconnecting with the same
opaque token replays the buffer and resumes live.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Dict, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

WS_CLOSE_PROCESS_EXITED = 4410
WS_CLOSE_SUPERSEDED = 4409
TUI_FORCE_REDRAW = b"\x0c"


def make_replay_request(generation: Optional[str] = None) -> tuple[str, bytes]:
    """Format a private OSC 777 redraw request for Ink TUI that does not insert text."""
    if not generation:
        generation = str(uuid.uuid4())
    return generation, f"\x1b]777;hermes-replay;request;{generation}\x07".encode("ascii")


class RingBuffer:
    """Keeps only the most recent ``capacity`` bytes appended to it."""

    def __init__(self, capacity: int) -> None:
        self._cap = capacity
        self._buf = bytearray()
        self._escape_tail = b""
        self.truncated = False

    def append(self, data: bytes) -> None:
        # If the incoming stream clears the terminal scrollback (\x1b[3J),
        # discard all buffered bytes prior to that clear. Keeping wiped
        # scrollback causes reconnected viewers to replay duplicate transcripts.
        # PTY read boundaries can split CSI 3 J at any byte. Inspect the
        # retained suffix too, without duplicating it when no clear is found.
        candidate = self._escape_tail + data
        self._escape_tail = candidate[-3:]
        clear_idx = candidate.rfind(b"\x1b[3J")
        if clear_idx != -1:
            self._buf.clear()
            self.truncated = False
            data = candidate[clear_idx:]
        self._buf.extend(data)
        overflow = len(self._buf) - self._cap
        if overflow > 0:
            del self._buf[:overflow]
            self.truncated = True

    def snapshot(self) -> bytes:
        return bytes(self._buf)


async def _close_ws(ws, code: int) -> None:
    try:
        if ws is not None:
            await ws.close(code=code)
    except Exception:
        pass


class PtySession:
    def __init__(self, key: str, bridge, *, buffer_cap: int, read_timeout: float) -> None:
        self.key = key
        self.bridge = bridge
        self.buffer = RingBuffer(buffer_cap)
        self.alive = True
        self.attached = False
        self._retiring = False
        self._busy_probed_logged = False
        self.last_detached_at: Optional[float] = None
        self._read_timeout = read_timeout
        self._viewers: Set[Any] = set()
        self._leader_ws: Optional[Any] = None
        self._attach_generation = 0
        self._drain_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()

    @property
    def _ws(self) -> Any:
        return self._leader_ws

    def is_leader(self, ws: Any) -> bool:
        return self._leader_ws is None or self._leader_ws is ws

    async def start(self) -> None:
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, self.bridge.read, self._read_timeout)
            if chunk is None:                       # EOF — the agent process exited
                self.alive = False
                viewers = list(self._viewers)
                self._viewers.clear()
                self._leader_ws = None
                for viewer in viewers:
                    await _close_ws(viewer, WS_CLOSE_PROCESS_EXITED)
                return
            if not chunk:                            # idle tick
                await asyncio.sleep(0.01)
                continue
            self.buffer.append(chunk)
            for viewer in list(self._viewers):
                try:
                    await viewer.send_bytes(chunk)
                except Exception:
                    self.detach(viewer)

    async def write(self, ws: Any, data: Union[str, bytes]) -> bool:
        """Serialize input to the PTY bridge. Genuine typing input promotes ws to leader."""
        async with self._write_lock:
            if not self.alive:
                return False
            if isinstance(data, (bytes, bytearray)):
                # Promote to leader on genuine user text typing (skip mouse tracking, resize, and control escapes)
                if data and not data.startswith(b"\x1b") and (data >= b" " or data in (b"\r", b"\n")):
                    self._leader_ws = ws
            elif isinstance(data, str):
                if data and not data.startswith("\x1b") and (data >= " " or data in ("\r", "\n")):
                    self._leader_ws = ws
                data = data.encode("utf-8")
            generation = self._attach_generation
            delivered = await self.bridge.write(data)
            if not delivered and self.is_leader(ws) and generation == self._attach_generation:
                # Backpressure is not process death. Otherwise the dead-remnant
                # cleanup path bypasses cooperative retirement for working agents.
                probe = getattr(self.bridge, "is_alive", None)
                if probe is not None and not probe():
                    self.alive = False
            return delivered

    async def attach(self, ws: Any, *, force_redraw: bool = False, generation: Optional[str] = None) -> bool:
        """Attach a browser terminal viewer and replay buffered PTY output without kicking out peers."""
        if self._retiring:
            return False
        self._busy_probed_logged = False
        self._viewers.add(ws)
        self._leader_ws = ws
        self._attach_generation += 1
        self.attached = True
        self.last_detached_at = None
        if snap := self.buffer.snapshot():
            try:
                await ws.send_bytes(snap)
            except Exception:
                # Client dropped mid-replay; the caller never reaches its writer loop, so undo the
                # attach here or reap_idle() can never reclaim this PTY (#110849).
                self.detach(ws)
                return False
        if generation is not None:
            _, payload = make_replay_request(generation)
            return await self.write(ws, payload)
        if force_redraw:
            return await self.write(ws, TUI_FORCE_REDRAW)
        return True

    def detach(self, ws: Any) -> None:
        self._viewers.discard(ws)
        if self._leader_ws is ws:
            self._leader_ws = next(iter(self._viewers), None)
        if not self._viewers:
            self.attached = False
            self.last_detached_at = time.monotonic()
            self._busy_probed_logged = False

    async def close(self) -> None:
        self.alive = False
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
        self._viewers.clear()
        self._leader_ws = None
        try:
            await asyncio.to_thread(self.bridge.close)
        except Exception:
            pass


class RegistryFull(Exception):
    """Admission refused because retained terminals cannot be safely reclaimed."""

    def __init__(self, message: str = "Too many chat terminals are open in other tabs; close one and try again.") -> None:
        super().__init__(message)


class MemoryBudgetFull(RegistryFull):
    """A new terminal cannot be admitted under the process-tree memory budget."""

    def __init__(
        self,
        message: str = "Terminal memory budget reached or unavailable; existing work is protected.",
        *,
        reason: str = "memory",
    ) -> None:
        super().__init__(message)
        self.rejection_reason = reason


async def run_reaper(registry: "PtySessionRegistry", *, interval: float = 30.0) -> None:
    """Periodically reap idle/dead keep-alive sessions. Cancelled on shutdown."""
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.reap_idle()
        except Exception:
            pass


class PtySessionRegistry:
    def __init__(self, *, ttl: float, max_sessions: int, buffer_cap: int, read_timeout: float,
                 memory_budget_bytes: int | None = None,
                 memory_usage: Callable[[list[PtySession]], int | None] | None = None) -> None:
        self._ttl = ttl
        self._max = max_sessions
        self._buffer_cap = buffer_cap
        self._read_timeout = read_timeout
        self._memory_budget_bytes = memory_budget_bytes
        self._memory_usage = memory_usage
        self._sessions: Dict[str, PtySession] = {}
        # Covers admission through completed retirement, not just the victim selection.
        self._attach_lock = asyncio.Lock()
        self._standby_session: Optional[PtySession] = None
        self._standby_lock = asyncio.Lock()
        self._standby_spawn_fn: Optional[Callable[[], object]] = None

    def configure_standby_spawn(self, spawn_fn: Callable[[], object]) -> None:
        self._standby_spawn_fn = spawn_fn

    async def ensure_standby(self) -> None:
        # A speculative process must not bypass memory admission or the terminal cap.
        if self._memory_budget_bytes is not None or self._standby_spawn_fn is None:
            return
        async with self._standby_lock:
            if self._standby_session is not None and self._standby_session.alive:
                return
            try:
                bridge = await asyncio.to_thread(self._standby_spawn_fn)
                session = PtySession("__standby__", bridge, buffer_cap=self._buffer_cap,
                                     read_timeout=self._read_timeout)
                await session.start()
                self._standby_session = session
            except Exception:
                self._standby_session = None

    @staticmethod
    def _safe_pid(session: PtySession) -> Optional[int]:
        bridge = getattr(session, "bridge", None)
        pid = getattr(bridge, "pid", None) if bridge is not None else None
        if pid is None:
            pid = getattr(session, "pid", None)
        return pid

    @staticmethod
    def _dead(session: PtySession) -> bool:
        probe = getattr(session.bridge, "is_alive", None)
        return not session.alive or (probe is not None and not probe())

    async def _remove(
        self,
        session: PtySession,
        *,
        reason: Optional[str] = None,
        now: Optional[float] = None,
        rss: Optional[int] = None,
        budget: Optional[int] = None,
    ) -> None:
        session._retiring = True
        self._sessions.pop(session.key, None)
        await session.close()
        if reason is not None:
            now_ts = time.monotonic() if now is None else now
            idle_duration = max(0.0, now_ts - session.last_detached_at) if session.last_detached_at is not None else 0.0
            pid = self._safe_pid(session)
            budget_val = budget if budget is not None else self._memory_budget_bytes
            logger.info(
                "PTY session evicted: reason=%s, pid=%s, idle_seconds=%.1f, "
                "registry_count=%d, rss_bytes=%s, budget_bytes=%s",
                reason,
                pid,
                idle_duration,
                len(self._sessions),
                rss,
                budget_val,
            )

    async def _retire(
        self,
        session: PtySession,
        now: float,
        *,
        reason: str = "ttl",
        rss: Optional[int] = None,
        budget: Optional[int] = None,
    ) -> bool:
        if session.attached or session.last_detached_at is None:
            return False
        probe = getattr(session.bridge, "retire_if_idle", None)
        if probe is None:
            return False
        # Reject a stale attach() caller while the gateway freezes admission.
        session._retiring = True
        async def settle_retirement() -> bool:
            try:
                approved = await asyncio.to_thread(probe)
            except Exception:
                approved = False
            if approved is True:
                await self._remove(session, reason=reason, now=now, rss=rss, budget=budget)
                return True
            # Busy/unknown must not be killed as soon as a long turn finishes.
            idle_duration = max(0.0, now - session.last_detached_at)
            session.last_detached_at = now
            if not getattr(session, "_busy_probed_logged", False):
                session._busy_probed_logged = True
                pid = self._safe_pid(session)
                logger.debug(
                    "PTY session busy probe: pid=%s busy_or_unknown, retaining; idle_seconds=%.1f",
                    pid,
                    idle_duration,
                )
            return False

        # Shield the entire prepare/commit/close transaction, not just the probe.
        # Releasing the admission lock after commit but before close loses both
        # the process and its memory accounting if the requesting socket vanishes.
        operation = asyncio.create_task(settle_retirement())
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            raise
        finally:
            if session.alive:
                session._retiring = False

    def _idle_candidates(self) -> list[PtySession]:
        return sorted((s for s in self._sessions.values()
                       if not s.attached and s.last_detached_at is not None),
                      key=lambda s: s.last_detached_at)

    async def _usage(self) -> int | None:
        if self._memory_usage is None:
            return None
        try:
            return await asyncio.to_thread(self._memory_usage, list(self._sessions.values()))
        except Exception:
            return None

    async def _check_memory_budget(self, now: float) -> Tuple[bool, Optional[str], Optional[int]]:
        if self._memory_budget_bytes is None:
            return True, None, None
        usage = await self._usage()
        if usage is None:
            return False, "memory_unknown", None
        for session in self._idle_candidates():
            if usage < self._memory_budget_bytes:
                return True, None, usage
            if await self._retire(session, now, reason="memory", rss=usage, budget=self._memory_budget_bytes):
                usage = await self._usage()
                if usage is None:
                    return False, "memory_unknown", None
        if usage < self._memory_budget_bytes:
            return True, None, usage
        return False, "memory", usage

    async def _reclaim_memory(self, now: float) -> bool:
        admitted, _, _ = await self._check_memory_budget(now)
        return admitted

    async def _reap_locked(self, now: float) -> None:
        if self._standby_session is not None and self._dead(self._standby_session):
            await self._standby_session.close()
            self._standby_session = None
        for session in list(self._sessions.values()):
            if self._dead(session):
                await self._remove(session, reason="dead", now=now)
            elif (not session.attached and session.last_detached_at is not None
                  and now - session.last_detached_at > self._ttl):
                await self._retire(session, now, reason="ttl")

    async def attach_or_spawn(self, key: str, *, spawn: Callable[[], object],
                              allow_standby: bool = True) -> Tuple[PtySession, bool]:
        async with self._attach_lock:
            existing = self._sessions.get(key)
            # Reconnection is not a new allocation, even under memory pressure.
            if existing is not None and not self._dead(existing):
                return existing, False
            now = time.monotonic()
            await self._reap_locked(now)
            if len(self._sessions) >= self._max:
                for victim in self._idle_candidates():
                    if await self._retire(victim, now, reason="capacity"):
                        break
                if len(self._sessions) >= self._max:
                    logger.warning(
                        "Terminal admission rejected: reason=capacity, registry_count=%d, max_sessions=%d",
                        len(self._sessions),
                        self._max,
                    )
                    raise RegistryFull("Terminal capacity reached; attached or working sessions are protected.")
            admitted, mem_rejection_reason, usage = await self._check_memory_budget(now)
            if not admitted:
                logger.warning(
                    "Terminal admission rejected: reason=%s, registry_count=%d, rss_bytes=%s, budget_bytes=%s",
                    mem_rejection_reason,
                    len(self._sessions),
                    usage,
                    self._memory_budget_bytes,
                )
                raise MemoryBudgetFull(reason=mem_rejection_reason or "memory")
            if allow_standby and self._standby_session is not None and self._standby_session.alive:
                async with self._standby_lock:
                    session = self._standby_session
                    self._standby_session = None
                    session.key = key
                    self._sessions[key] = session
                    asyncio.create_task(self.ensure_standby())
                    return session, True
            # Finish a fork even if its requesting socket disappears; do not orphan it.
            operation = asyncio.create_task(asyncio.to_thread(spawn))
            try:
                bridge = await asyncio.shield(operation)
            except asyncio.CancelledError:
                bridge = await operation
                await asyncio.to_thread(bridge.close)
                raise
            session = PtySession(key, bridge, buffer_cap=self._buffer_cap, read_timeout=self._read_timeout)
            await session.start()
            self._sessions[key] = session
            if allow_standby and self._standby_spawn_fn is not None:
                asyncio.create_task(self.ensure_standby())
            return session, True

    def detach(self, key: str, ws) -> None:
        session = self._sessions.get(key)
        if session is not None:
            session.detach(ws)

    async def reap_idle(self, now: Optional[float] = None) -> None:
        async with self._attach_lock:
            now = time.monotonic() if now is None else now
            await self._reap_locked(now)
            await self._reclaim_memory(now)

    async def close_all(self) -> None:
        # Explicit application shutdown differs from idle/capacity reclamation.
        async with self._attach_lock:
            if self._standby_session is not None:
                await self._standby_session.close()
                self._standby_session = None
            for session in list(self._sessions.values()):
                await self._remove(session)
