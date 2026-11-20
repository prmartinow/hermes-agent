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


def publish_turn_start(agent: Any, user_message: Any) -> None:
    """Notify viewers of a new turn without making the stream a turn dependency."""
    writer = getattr(agent, "_live_stream_writer", None)
    if writer is None:
        return
    try:
        writer.turn_start(user_message)
    except Exception:
        logger.exception("Could not publish the live session start")


def publish_message_complete(agent: Any, text: str) -> None:
    """Notify external viewers without letting an unavailable stream break the turn."""
    writer = getattr(agent, "_live_stream_writer", None)
    if writer is None:
        return
    try:
        writer.message_complete(text or "")
    except Exception:
        logger.exception("Could not publish live message completion")


def publish_tool_start(agent: Any, call_id: str, name: str, display_args: dict) -> None:
    writer = getattr(agent, "_live_stream_writer", None)
    if writer is None:
        return
    try:
        from agent.tool_executor import _build_tool_preview
        writer.tool_start(call_id, name, display_args, preview=_build_tool_preview(name, display_args))
    except Exception:
        logger.exception("Could not publish live tool start")


def publish_tool_complete(agent: Any, ref: Any, result: Any, blocked: bool) -> None:
    writer = getattr(agent, "_live_stream_writer", None)
    if blocked or writer is None:
        return
    try:
        from agent.tool_executor import _multimodal_text_summary, _redact_tool_args_for_display
        display_args = _redact_tool_args_for_display(ref.name, ref.args) or ref.args
        writer.tool_complete(ref.call_id, ref.name, display_args, result, summary=_multimodal_text_summary(result))
    except Exception:
        logger.exception("Could not publish live tool completion")


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
    except OSError:
        logger.debug("Cannot create live-session directory", exc_info=True)
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
        except Exception:  # health: allow BLE001 -- redaction must fail closed for any unexpected failure.
            return "[redaction unavailable]"
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

    def write_event(self, event_type: str, payload: Optional[dict[str, Any]] = None) -> None:
        if self._disabled or self._closed or not self._path:
            return
        try:
            with self._lock:
                self._seq += 1
                record: dict[str, Any] = {
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
        except Exception:
            logger.debug("LiveSessionStreamWriter failed to write event %s", event_type, exc_info=True)

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
        args: Optional[dict[str, Any]] = None,
        preview: Optional[str] = None,
    ) -> None:
        payload: dict[str, Any] = {
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
        args: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        payload: dict[str, Any] = {}
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
        args: Optional[dict[str, Any]] = None,
        result: Any = None,
        summary: Optional[str] = None,
        duration_s: Optional[float] = None,
        inline_diff: Optional[str] = None,
    ) -> None:
        payload: dict[str, Any] = {
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

    def steer(self, text: str) -> None:
        if text:
            self.write_event("turn.steer", {"text": str(text)})

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

        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
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
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue

        if not turn_active:
            return False

        if last_pid is not None:
            try:
                from gateway.status import _pid_exists

                if not _pid_exists(last_pid):
                    return False
            except Exception:
                logger.debug("Could not check live-session writer process", exc_info=True)
                return False

        return True
    except (OSError, ValueError):
        logger.debug("Could not read live-session activity", exc_info=True)
        return False


def read_live_session_events(
    session_id: str,
    profile_home: Optional[Path | str] = None,
    from_offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Read structured events from the given byte offset."""
    path = get_live_session_stream_path(session_id, profile_home)
    if not path.is_file():
        return [], from_offset
    events = []
    new_offset = from_offset
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            f.seek(from_offset)
            while line := f.readline():
                line_str = line.strip()
                if line_str:
                    try:
                        events.append(json.loads(line_str))
                    except json.JSONDecodeError:
                        logger.debug("Ignoring malformed live-session event", exc_info=True)
            new_offset = f.tell()
    except (OSError, ValueError):
        logger.debug("read_live_session_events failed for %s", session_id, exc_info=True)
    return events, new_offset


class LiveSessionStreamWatcher:
    """Tails a live session's .jsonl file and emits events to a client."""

    def __init__(
        self,
        session_id: str,
        client_sid: str,
        emit_fn: Callable[[str, str, Optional[dict[str, Any]]], None],
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
        from agent.memory_provider import spawn_context_thread

        self._thread = spawn_context_thread(
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

    def _process_line(self, line: str, last_pid):
        if not line.strip():
            return last_pid, False
        try:
            event = json.loads(line.strip())
            last_pid = event.get("pid") or last_pid
            self._dispatch_event(event)
            if event.get("type") in ("turn.complete", "message.complete"):
                if self.on_idle:
                    self.on_idle()
                return last_pid, True
        except Exception:
            logger.debug("Could not dispatch live-session event", exc_info=True)
        return last_pid, False

    def _replay(self):
        if not self._path.is_file():
            return 0, None, False
        with open(self._path, "r", encoding="utf-8-sig", errors="replace") as stream:
            lines = stream.readlines()
        last_turn = 0
        for idx, line in enumerate(lines):
            try:
                if json.loads(line.strip()).get("type") == "turn.start":
                    last_turn = idx
            except (json.JSONDecodeError, AttributeError, TypeError):
                logger.debug("Ignoring malformed live-session replay event", exc_info=True)
        last_pid = None
        with open(self._path, "r", encoding="utf-8-sig", errors="replace") as stream:
            for idx in range(last_turn):
                if not stream.readline():
                    break
            while line := stream.readline():
                last_pid, complete = self._process_line(line, last_pid)
                if complete:
                    return stream.tell(), last_pid, True
            return stream.tell(), last_pid, False

    def _tail(self, file_offset, last_pid):
        activity = False
        with open(self._path, "r", encoding="utf-8-sig", errors="replace") as stream:
            stream.seek(file_offset)
            while not self._stop_event.is_set():
                line = stream.readline()
                if not line:
                    break
                activity = True
                last_pid, complete = self._process_line(line, last_pid)
                if complete:
                    return stream.tell(), last_pid, activity, True
            return stream.tell(), last_pid, activity, False

    def _run(self) -> None:
        file_offset = 0
        last_activity = time.time()
        last_pid = None
        try:
            file_offset, last_pid, complete = self._replay()
            if complete:
                return
        except Exception:
            logger.debug("Error initializing live stream replay for %s", self.session_id, exc_info=True)

        while not self._stop_event.is_set():
            if not self._path.is_file():
                time.sleep(0.1)
                if time.time() - last_activity > 10.0:
                    break
                continue
            try:
                file_offset, last_pid, activity, complete = self._tail(file_offset, last_pid)
                if activity:
                    last_activity = time.time()
                if complete:
                    return
            except Exception:
                logger.debug("Error tailing live stream for %s", self.session_id, exc_info=True)

            if last_pid is not None:
                try:
                    from gateway.status import _pid_exists

                    if not _pid_exists(last_pid):
                        logger.debug("Live stream writer process %s died; ending watcher for %s", last_pid, self.session_id)
                        break
                except Exception:
                    logger.debug("Could not check live-session writer process", exc_info=True)
                    break
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
        except Exception:
            logger.debug("LiveSessionStreamWatcher dispatch failed for event %s", ev_type, exc_info=True)
