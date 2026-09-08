"""Keep-alive PTY sessions for dashboard terminals.

A PTY process outlives the WebSocket that created it: a single drain task always reads the PTY into
a bounded RingBuffer and forwards to the attached socket when present. Reconnecting with the same
opaque token replays the buffer and resumes live.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional, Set, Tuple, Union

WS_CLOSE_PROCESS_EXITED = 4410
WS_CLOSE_SUPERSEDED = 4409
TUI_FORCE_REDRAW = b"\x0c"


class RingBuffer:
    """Keeps only the most recent ``capacity`` bytes appended to it."""

    def __init__(self, capacity: int) -> None:
        self._cap = capacity
        self._buf = bytearray()
        self.truncated = False

    def append(self, data: bytes) -> None:
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
        self.last_detached_at: Optional[float] = None
        self._read_timeout = read_timeout
        self._viewers: Set[Any] = set()
        self._leader_ws: Optional[Any] = None
        self._attach_generation = 0
        self._drain_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()

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
            dead_viewers = []
            for viewer in list(self._viewers):
                try:
                    await viewer.send_bytes(chunk)
                except Exception:
                    dead_viewers.append(viewer)
            for dv in dead_viewers:
                self._viewers.discard(dv)
                if self._leader_ws is dv:
                    self._leader_ws = next(iter(self._viewers), None)
            if not self._viewers:
                self.attached = False
                if self.last_detached_at is None:
                    self.last_detached_at = time.monotonic()

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
            delivered = await self.bridge.write(data)
            if not delivered and self.is_leader(ws):
                self.alive = False
            return delivered

    async def attach(self, ws: Any, *, force_redraw: bool = False) -> bool:
        """Attach a browser terminal viewer and replay buffered PTY output without kicking out peers."""
        self._viewers.add(ws)
        if self._leader_ws is None:
            self._leader_ws = ws
        self._attach_generation += 1
        self.attached = True
        self.last_detached_at = None
        if snap := self.buffer.snapshot():
            await ws.send_bytes(snap)
        if force_redraw:
            return await self.bridge.write(TUI_FORCE_REDRAW)
        return True

    def detach(self, ws: Any) -> None:
        self._viewers.discard(ws)
        if self._leader_ws is ws:
            self._leader_ws = next(iter(self._viewers), None)
        if not self._viewers:
            self.attached = False
            self.last_detached_at = time.monotonic()

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
    pass


async def run_reaper(registry: "PtySessionRegistry", *, interval: float = 30.0) -> None:
    """Periodically reap idle/dead keep-alive sessions. Cancelled on shutdown."""
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.reap_idle()
        except Exception:
            pass


class PtySessionRegistry:
    def __init__(self, *, ttl: float, max_sessions: int, buffer_cap: int, read_timeout: float) -> None:
        self._ttl = ttl
        self._max = max_sessions
        self._buffer_cap = buffer_cap
        self._read_timeout = read_timeout
        self._sessions: Dict[str, PtySession] = {}
        self._standby_session: Optional[PtySession] = None
        self._standby_lock = asyncio.Lock()
        self._standby_spawn_fn: Optional[Callable[[], object]] = None

    def configure_standby_spawn(self, spawn_fn: Callable[[], object]) -> None:
        self._standby_spawn_fn = spawn_fn

    async def ensure_standby(self) -> None:
        """Pre-spawn one warm standby PTY worker if none exists."""
        if self._standby_session is not None and self._standby_session.alive:
            return
        if self._standby_spawn_fn is None:
            return
        async with self._standby_lock:
            if self._standby_session is not None and self._standby_session.alive:
                return
            try:
                bridge = await asyncio.to_thread(self._standby_spawn_fn)
                session = PtySession("__standby__", bridge,
                                     buffer_cap=self._buffer_cap,
                                     read_timeout=self._read_timeout)
                await session.start()
                self._standby_session = session
            except Exception:
                # Standby failure should not crash dashboard; fallback to on-demand spawn
                self._standby_session = None

    async def attach_or_spawn(self, key: str, *, spawn: Callable[[], object],
                              allow_standby: bool = True) -> Tuple[PtySession, bool]:
        await self.reap_idle()
        existing = self._sessions.get(key)
        if existing is not None and existing.alive:
            # Actively verify the underlying PTY child process is alive (not dead/zombie/hung)
            is_alive_fn = getattr(existing.bridge, "is_alive", None)
            if is_alive_fn is not None and not is_alive_fn():
                existing.alive = False
            else:
                return existing, False
        if existing is not None:                       # dead remnant
            await existing.close()
            self._sessions.pop(key, None)
        if len(self._sessions) >= self._max:
            self._reap_one_idle_or_raise()

        # Claim standby worker if available and eligible
        if allow_standby and self._standby_session is not None and self._standby_session.alive:
            async with self._standby_lock:
                if self._standby_session is not None and self._standby_session.alive:
                    session = self._standby_session
                    self._standby_session = None
                    session.key = key
                    self._sessions[key] = session
                    # Replenish pool in background
                    if self._standby_spawn_fn is not None:
                        asyncio.create_task(self.ensure_standby())
                    return session, True

        # PTY spawn does blocking fork/exec work — keep it off the event loop (#53227).
        bridge = await asyncio.to_thread(spawn)
        session = PtySession(key, bridge, buffer_cap=self._buffer_cap, read_timeout=self._read_timeout)
        await session.start()
        self._sessions[key] = session
        # Ensure standby worker is stocked
        if allow_standby and self._standby_spawn_fn is not None:
            asyncio.create_task(self.ensure_standby())
        return session, True

    def detach(self, key: str, ws) -> None:
        s = self._sessions.get(key)
        if s is not None:
            s.detach(ws)

    async def reap_idle(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if self._standby_session is not None and not self._standby_session.alive:
            standby = self._standby_session
            self._standby_session = None
            try:
                await standby.close()
            except Exception:
                pass
            if self._standby_spawn_fn is not None:
                asyncio.create_task(self.ensure_standby())

        doomed = [
            key for key, s in self._sessions.items()
            if not s.alive or (hasattr(s.bridge, "is_alive") and not s.bridge.is_alive()) or (not s.attached and s.last_detached_at is not None and (now - s.last_detached_at) > self._ttl)
        ]
        for key in doomed:
            await self._sessions.pop(key).close()

    def _reap_one_idle_or_raise(self) -> None:
        idle = [s for s in self._sessions.values() if not s.attached and s.last_detached_at is not None]
        if not idle:
            raise RegistryFull()
        oldest = min(idle, key=lambda s: s.last_detached_at or 0.0)
        self._sessions.pop(oldest.key, None)
        asyncio.create_task(oldest.close())

    async def close_all(self) -> None:
        if self._standby_session is not None:
            standby = self._standby_session
            self._standby_session = None
            try:
                await standby.close()
            except Exception:
                pass
        for key in list(self._sessions):
            await self._sessions.pop(key).close()
