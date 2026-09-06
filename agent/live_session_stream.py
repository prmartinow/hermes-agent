"""Live session event stream for cross-process activity broadcasting.

Allows any Hermes CLI or Agent turn (interactive, background, -q, --query-file,
kanban worker, cron, etc.) to record append-only JSON-RPC style live stream events
into:
    <hermes_home>/runtime/live_sessions/<session_id>.jsonl

This enables the Web TUI, Web Dashboard (/chat?resume=<id>), and Desktop app
to attach to and stream thoughts, tool calls, and text deltas in real-time even
when the agent process runs externally in the background.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Max idle seconds before considering an unfinalized external session stale
LIVE_SESSION_MAX_IDLE_S = 120.0


def _safe_session_id(session_id: str) -> str:
    """Sanitize session_id into a safe filename component."""
    safe = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", str(session_id or "").strip())
    return safe or "unnamed_session"


def get_live_session_dir(profile_home: Optional[Path | str] = None) -> Path:
    """Directory holding live session stream files."""
    home = Path(profile_home) if profile_home else get_hermes_home()
    d = home / "runtime" / "live_sessions"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def get_live_session_stream_path(
    session_id: str, profile_home: Optional[Path | str] = None
) -> Path:
    """Return path to <hermes_home>/runtime/live_sessions/<session_id>.jsonl."""
    safe = _safe_session_id(session_id)
    return get_live_session_dir(profile_home) / f"{safe}.jsonl"


def _redact_obj(obj: Any) -> Any:
    """Recursively redact secrets in event payload strings."""
    if isinstance(obj, str):
        try:
            from agent.redact import redact_sensitive_text

            return redact_sensitive_text(obj, force=True) or ""
        except Exception:
            return obj
    elif isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    elif isinstance(obj, tuple):
        return tuple(_redact_obj(x) for x in obj)
    return obj


class LiveSessionStreamWriter:
    """Thread-safe append-only live event writer for one session."""

    def __init__(self, session_id: str, profile_home: Optional[Path | str] = None):
        self.session_id = str(session_id or "").strip()
        self.profile_home = Path(profile_home) if profile_home else None
        self._path = get_live_session_stream_path(self.session_id, self.profile_home) if self.session_id else None
        self._lock = threading.Lock()
        self._seq = 0
        self._turn_active = False
        self._closed = False
        self._disabled = not bool(self.session_id)

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def write_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if self._disabled or self._closed or not self._path:
            return
        try:
            with self._lock:
                self._seq += 1
                record: Dict[str, Any] = {
                    "ts": round(time.time(), 4),
                    "type": str(event_type),
                    "seq": self._seq,
                    "pid": os.getpid(),
                }
                if payload is not None:
                    record["payload"] = _redact_obj(payload)

                line = json.dumps(record, ensure_ascii=False)
                if not self._path.parent.exists():
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line + "\n")
                    f.flush()
        except Exception as exc:
            logger.debug("LiveSessionStreamWriter failed to write event %s: %s", event_type, exc)

    def turn_start(self, user_message: Any = None) -> None:
        self._turn_active = True
        msg_text = user_message if isinstance(user_message, str) else str(user_message or "")
        self.write_event("turn.start", {"user_message": msg_text})
        self.write_event("message.start", {"role": "assistant"})

    def thinking_delta(self, text: str) -> None:
        if text:
            self.write_event("thinking.delta", {"text": text})

    def reasoning_delta(self, text: str) -> None:
        if text:
            self.write_event("reasoning.delta", {"text": text})

    def message_delta(self, text: str) -> None:
        if text:
            self.write_event("message.delta", {"text": text})

    def tool_generating(self, name: str) -> None:
        if name:
            self.write_event("tool.generating", {"name": name})

    def tool_start(
        self,
        tool_id: str,
        name: str,
        args: Optional[Dict[str, Any]] = None,
        preview: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "tool_id": str(tool_id or ""),
            "name": str(name or ""),
            "args": args or {},
        }
        if preview:
            payload["preview"] = str(preview)
        self.write_event("tool.start", payload)

    def tool_progress(
        self,
        event_type: str,
        name: Optional[str] = None,
        preview: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        payload: Dict[str, Any] = {}
        if name:
            payload["name"] = str(name)
        if preview:
            payload["preview"] = str(preview)
            payload["text"] = str(preview)
        if args:
            payload["args"] = args
        for k, v in kwargs.items():
            if v is not None:
                payload[k] = v
        self.write_event(event_type or "tool.progress", payload)

    def tool_complete(
        self,
        tool_id: str,
        name: str,
        args: Optional[Dict[str, Any]] = None,
        result: Any = None,
        summary: Optional[str] = None,
        duration_s: Optional[float] = None,
        inline_diff: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "tool_id": str(tool_id or ""),
            "name": str(name or ""),
            "args": args or {},
        }
        if result is not None:
            payload["result"] = result
        if summary:
            payload["summary"] = str(summary)
        if duration_s is not None:
            payload["duration_s"] = float(duration_s)
        if inline_diff:
            payload["inline_diff"] = str(inline_diff)
        self.write_event("tool.complete", payload)

    def message_complete(self, text: str = "") -> None:
        self.write_event("message.complete", {"text": str(text or "")})
        self.write_event("turn.complete", {})
        self._turn_active = False

    def close(self) -> None:
        if self._turn_active:
            self.message_complete("")
        self._closed = True


def is_live_session_active(
    session_id: str,
    profile_home: Optional[Path | str] = None,
    max_idle_s: float = LIVE_SESSION_MAX_IDLE_S,
) -> bool:
    """Check if an external process has an active (unclosed) turn streaming."""
    if not session_id:
        return False
    path = get_live_session_stream_path(session_id, profile_home)
    if not path.is_file():
        return False
    try:
        stat = path.stat()
        mtime = stat.st_mtime
        if time.time() - mtime > max_idle_s:
            return False

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if not lines:
            return False

        last_pid = None
        turn_active = False
        for line in lines[-50:]:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                ev = json.loads(line_str)
                ev_type = ev.get("type")
                if ev.get("pid"):
                    last_pid = ev.get("pid")
                if ev_type in ("turn.start", "message.start"):
                    turn_active = True
                elif ev_type in ("turn.complete", "message.complete"):
                    turn_active = False
            except Exception:
                continue

        if not turn_active:
            return False

        if last_pid is not None:
            try:
                from gateway.status import _pid_exists

                if not _pid_exists(last_pid):
                    return False
            except Exception:
                pass

        return True
    except Exception:
        return False


def read_live_session_events(
    session_id: str,
    profile_home: Optional[Path | str] = None,
    from_offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Read structured events from the given byte offset."""
    path = get_live_session_stream_path(session_id, profile_home)
    if not path.is_file():
        return [], from_offset
    events = []
    new_offset = from_offset
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(from_offset)
            for line in f:
                line_str = line.strip()
                if line_str:
                    try:
                        events.append(json.loads(line_str))
                    except Exception:
                        pass
            new_offset = f.tell()
    except Exception as exc:
        logger.debug("read_live_session_events failed for %s: %s", session_id, exc)
    return events, new_offset


class LiveSessionStreamWatcher:
    """Tails a live session's .jsonl file and emits events to a client."""

    def __init__(
        self,
        session_id: str,
        client_sid: str,
        emit_fn: Callable[[str, str, Optional[Dict[str, Any]]], None],
        profile_home: Optional[Path | str] = None,
        on_idle: Optional[Callable[[], None]] = None,
    ):
        self.session_id = str(session_id)
        self.client_sid = str(client_sid)
        self.emit_fn = emit_fn
        self.profile_home = profile_home
        self.on_idle = on_idle
        self._path = get_live_session_stream_path(self.session_id, self.profile_home)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"live-stream-watcher-{self.client_sid}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        file_offset = 0
        last_activity = time.time()
        last_pid = None

        try:
            if self._path.is_file():
                with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                last_turn_idx = 0
                for idx, line in enumerate(lines):
                    try:
                        ev = json.loads(line.strip())
                        if ev.get("type") == "turn.start":
                            last_turn_idx = idx
                    except Exception:
                        pass

                with open(self._path, "r", encoding="utf-8", errors="replace") as f_cur:
                    cur_line_idx = 0
                    for line in f_cur:
                        if cur_line_idx >= last_turn_idx:
                            line_str = line.strip()
                            if line_str:
                                try:
                                    ev = json.loads(line_str)
                                    self._dispatch_event(ev)
                                    if ev.get("pid"):
                                        last_pid = ev.get("pid")
                                    if ev.get("type") in ("turn.complete", "message.complete"):
                                        if self.on_idle:
                                            self.on_idle()
                                        return
                                except Exception:
                                    pass
                        cur_line_idx += 1
                    file_offset = f_cur.tell()
        except Exception as exc:
            logger.debug("Error initializing live stream replay for %s: %s", self.session_id, exc)

        while not self._stop_event.is_set():
            if not self._path.is_file():
                time.sleep(0.1)
                if time.time() - last_activity > 10.0:
                    break
                continue

            try:
                with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(file_offset)
                    while not self._stop_event.is_set():
                        line = f.readline()
                        if not line:
                            file_offset = f.tell()
                            break
                        last_activity = time.time()
                        line_str = line.strip()
                        if not line_str:
                            continue
                        try:
                            ev = json.loads(line_str)
                            ev_type = ev.get("type")
                            if ev.get("pid"):
                                last_pid = ev.get("pid")
                            self._dispatch_event(ev)
                            if ev_type in ("turn.complete", "message.complete"):
                                if self.on_idle:
                                    self.on_idle()
                                return
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug("Error tailing live stream for %s: %s", self.session_id, exc)

            if last_pid is not None:
                try:
                    from gateway.status import _pid_exists

                    if not _pid_exists(last_pid):
                        logger.debug("Live stream writer process %s died; ending watcher for %s", last_pid, self.session_id)
                        break
                except Exception:
                    pass

            if time.time() - last_activity > LIVE_SESSION_MAX_IDLE_S:
                logger.debug("Live stream watcher timed out for %s", self.session_id)
                break

            time.sleep(0.05)

        if self.on_idle:
            self.on_idle()

    def _dispatch_event(self, ev: dict) -> None:
        ev_type = ev.get("type")
        payload = ev.get("payload")
        if not ev_type:
            return
        if ev_type in ("turn.start", "turn.complete"):
            return
        try:
            self.emit_fn(ev_type, self.client_sid, payload)
        except Exception as exc:
            logger.debug("LiveSessionStreamWatcher dispatch failed for event %s: %s", ev_type, exc)
