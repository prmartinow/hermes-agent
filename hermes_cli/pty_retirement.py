"""Private, process-bound retirement control for dashboard-owned stdio gateways.

This is not the terminal byte stream. The socket lives in a bridge-created 0700
folder; only a same-user ancestor may request retirement. The client verifies the
kernel-reported peer PID belongs to its PTY before sending any mutating request.
Unsupported peer credentials or unreadable state conservatively retain the PTY.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading

import psutil

from hermes_cli.backend_retirement import retirement

_LIMIT = 4096
_TIMEOUT = 2.0


@contextmanager
def _socket_address(path: str):
    # pytest/profile scratch paths can exceed sockaddr_un.sun_path. Resolve the
    # owned directory via an fd, without chdir or a shared global short-path alias.
    target = Path(path)
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield f"/proc/self/fd/{fd}/{target.name}"
    finally:
        os.close(fd)


def _peer(sock):
    if not hasattr(socket, "SO_PEERCRED"):
        raise OSError("process-bound peer credentials unavailable")
    pid, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid() or pid <= 0:
        raise OSError("foreign retirement peer")
    return pid


def _ancestor(pid: int, ancestor: int) -> bool:
    return any(p.pid == ancestor for p in psutil.Process(pid).parents())


def _receive(stream):
    line = stream.readline(_LIMIT + 1)
    if len(line) > _LIMIT or not line.endswith(b"\n"):
        raise ValueError("invalid retirement frame")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("invalid retirement payload")
    return value


def _send(stream, payload):
    stream.write(json.dumps(payload).encode() + b"\n")
    stream.flush()


class _RetirementServer:
    def __init__(self, path: str):
        self.path = Path(path)
        parent = self.path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise OSError("retirement socket needs an owned private directory")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Never remove somebody else's live/stale socket to take over its identity.
            with _socket_address(path) as address:
                self.socket.bind(address)
            self.path.chmod(0o600)
            self.socket.listen(4)
            self.socket.settimeout(.25)
        except BaseException:
            self.socket.close()
            raise
        self.stopped = threading.Event()
        from agent.memory_provider import spawn_context_thread
        self.thread = spawn_context_thread(target=self._serve, daemon=True, name="pty-retirement")
        self.thread.start()

    def _serve(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(_TIMEOUT)
                    if not _ancestor(os.getpid(), _peer(conn)):
                        continue
                    with conn.makefile("rwb") as stream:
                        request = _receive(stream)
                        handlers = {
                            "prepare": retirement.prepare,
                            "commit": lambda: retirement.commit(request.get("token")),
                            "cancel": lambda: retirement.cancel(request.get("token")),
                        }
                        handler = handlers.get(request.get("action"))
                        _send(stream, handler() if handler else {"ok": False})
                except (OSError, ValueError, psutil.Error):
                    continue

    def close(self):
        self.stopped.set()
        self.socket.close()
        self.thread.join(timeout=_TIMEOUT + .5)
        self.path.unlink(missing_ok=True)


def start_retirement_server(path: str):
    server = _RetirementServer(path)
    atexit.register(server.close)
    return server


def _request(path: str, parent: int, payload: dict):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(_TIMEOUT)
        with _socket_address(path) as address:
            conn.connect(address)
        if not _ancestor(_peer(conn), parent):
            raise OSError("retirement peer does not belong to this PTY")
        with conn.makefile("rwb") as stream:
            _send(stream, payload)
            return _receive(stream)


def request_idle_retirement(path: str, *, expected_parent_pid: int) -> bool:
    token = None
    try:
        prepared = _request(path, expected_parent_pid, {"action": "prepare"})
        if prepared.get("ok") is not True or prepared.get("idle") is not True:
            return False
        token = prepared.get("token")
        if not isinstance(token, str) or not token:
            return False
        # Commit is idempotent, including after a lost response.
        for attempt in range(2):
            try:
                return _request(path, expected_parent_pid, {"action": "commit", "token": token}).get("ok") is True
            except (OSError, ValueError, psutil.Error):
                if attempt:
                    raise
    except (OSError, ValueError, psutil.Error):
        if token:
            try:
                _request(path, expected_parent_pid, {"action": "cancel", "token": token})
            except (OSError, ValueError, psutil.Error):
                pass  # Uncommitted prepare permits expire in the existing fence.
    return False
