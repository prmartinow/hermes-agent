"""Conservative resident-memory accounting for dashboard-owned terminals.

Count the hosting process once: it owns replay buffers, temporary snapshots and
WebSocket queues. Counting only buffer lengths misses those allocations. Sum
unique PTY process trees as well; shared resident pages may be counted twice,
so this is a soft admission/reclamation budget, never an OOM kill threshold.
"""
from __future__ import annotations

import os

import psutil


def pty_memory_usage(sessions) -> int | None:
    """Return aggregate RSS, or unknown when a live process cannot be measured.

    Bridges carry their child's birth time so PID reuse cannot attribute another
    process to a retained terminal. Already exited children contribute nothing.
    """
    try:
        host = psutil.Process(os.getpid())
        processes = {(host.pid, host.create_time()): host}
        for session in sessions:
            bridge = session.bridge
            pid = getattr(bridge, "pid", None)
            born = getattr(bridge, "process_birth_time", None)
            if pid is None or born is None:
                return None
            try:
                root = psutil.Process(pid)
                if root.create_time() != born:
                    return None
                for process in [root, *root.children(recursive=True)]:
                    processes[(process.pid, process.create_time())] = process
            except psutil.NoSuchProcess:
                continue
        total = 0
        for process in processes.values():
            try:
                total += process.memory_info().rss
            except psutil.NoSuchProcess:
                continue
        return total
    except (psutil.Error, OSError):
        # Unknown usage must not admit unbounded new terminals.
        return None
