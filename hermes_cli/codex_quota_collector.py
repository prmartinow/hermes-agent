"""Codex quota collector unit.

Fetches the latest rate limit snapshot from the OpenAI Codex backend
and records sanitized observations into the local SQLite quota ledger.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional, Union

import httpx

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

from agent.account_usage import (
    _codex_backend_urls,
    _codex_headers,
    _get_json,
    _resolve_codex_usage_credentials,
)
from hermes_cli.codex_quota_snapshots import (
    CodexQuotaSnapshotRecord,
    record_codex_quota_snapshot,
)

logger = logging.getLogger(__name__)


def _format_reset_at(val: Any) -> Optional[str]:
    """Convert numeric epoch timestamps or ISO strings into normalized ISO 8601 UTC string."""
    if val is None:
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
    if isinstance(val, str):
        s = val.strip()
        return s if s else None
    return None


def _map_window(window: Any) -> Optional[Dict[str, Any]]:
    """Map raw backend quota window dictionary to sanitized observation window dict."""
    if not isinstance(window, dict):
        return None
    used_percent = window.get("used_percent")
    reset_at = _format_reset_at(window.get("reset_at"))
    window_seconds = window.get("limit_window_seconds")
    if window_seconds is None:
        window_seconds = window.get("window_seconds")
    return {
        "used_percent": used_percent,
        "reset_at": reset_at,
        "window_seconds": window_seconds,
    }


def resolve_collector_home(
    profile: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Resolve the canonical HERMES_HOME directory for a profile or database path."""
    raw_profile = (profile or "").strip()
    if raw_profile and raw_profile.lower() not in ("default", "current"):
        try:
            from hermes_cli import profiles as profiles_mod
            canon = profiles_mod.normalize_profile_name(raw_profile)
            if canon != "default":
                return profiles_mod.get_profile_dir(canon).resolve()
        except Exception:
            pass
        try:
            p = Path(raw_profile)
            if p.is_dir() or p.is_absolute():
                return p.resolve()
        except Exception:
            pass
    if db_path is not None and not raw_profile:
        return Path(db_path).resolve().parent
    from hermes_constants import get_hermes_home
    return get_hermes_home().resolve()


def _acquire_collector_lease(lock_path: Path) -> Any:
    """Acquire non-blocking interprocess exclusive lock lease using repository flock convention."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
            lock_path.write_text(" ", encoding="utf-8")
        fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
        if fcntl:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        return fd
    except (BlockingIOError, OSError):
        if "fd" in locals() and fd is not None:
            with contextlib.suppress(Exception):
                fd.close()
        return None
    except Exception as exc:
        logger.debug("Failed to acquire collector lock %s: %s", lock_path, exc)
        if "fd" in locals() and fd is not None:
            with contextlib.suppress(Exception):
                fd.close()
        return None


def _release_collector_lease(fd: Any) -> None:
    """Release interprocess exclusive lock lease."""
    if fd is None:
        return
    try:
        if not fd.closed:
            if fcntl:
                with contextlib.suppress(Exception):
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            elif msvcrt:
                with contextlib.suppress(Exception):
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            fd.close()


@dataclass
class _CollectorHandle:
    home: Path
    stop_event: threading.Event
    lock_fd: Any
    thread: Optional[threading.Thread] = None
    profile: Optional[str] = None
    db_path: Optional[Union[str, Path]] = None


_collectors_lock = threading.Lock()
_active_collectors: Dict[Path, _CollectorHandle] = {}


def _run_collector_loop(
    resolved_home: Path,
    stop_event: threading.Event,
    interval_seconds: float,
    profile: Optional[str],
    db_path: Optional[Union[str, Path]],
    base_url: Optional[str],
    api_key: Optional[str],
    max_retries: int = 3,
) -> None:
    """Periodic collection loop with bounded retry and event wait."""
    while not stop_event.is_set():
        for attempt in range(max_retries):
            if stop_event.is_set():
                break
            try:
                collect_once(
                    db_path=db_path,
                    profile=profile,
                    base_url=base_url,
                    api_key=api_key,
                )
                break
            except Exception as exc:
                logger.debug(
                    "Quota collection error in %s (attempt %d/%d): %s",
                    resolved_home,
                    attempt + 1,
                    max_retries,
                    exc,
                )
                if attempt + 1 < max_retries:
                    if stop_event.wait(1.0):
                        return
        if stop_event.wait(interval_seconds):
            break


def start_codex_quota_collector(
    profile: Optional[str] = None,
    *,
    db_path: Optional[Union[str, Path]] = None,
    interval_seconds: float = 60.0,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_retries: int = 3,
) -> bool:
    """Start periodic background quota collection for the resolved profile / home.

    Enforces singleton execution per resolved home both within this process and
    across processes using an exclusive flock lease file.

    Returns:
        True if started, False if already running or lease acquisition failed.
    """
    resolved_home = resolve_collector_home(profile=profile, db_path=db_path)
    lock_path = resolved_home / ".codex_quota_collector.lock"

    with _collectors_lock:
        existing = _active_collectors.get(resolved_home)
        if existing is not None:
            if existing.thread is not None and existing.thread.is_alive():
                logger.debug("Codex quota collector already running for %s", resolved_home)
                return False
            # Clean up dead previous instance
            _release_collector_lease(existing.lock_fd)
            existing.lock_fd = None
            if _active_collectors.get(resolved_home) is existing:
                _active_collectors.pop(resolved_home, None)

        lock_fd = _acquire_collector_lease(lock_path)
        if lock_fd is None:
            logger.debug("Could not acquire interprocess collector lease for %s", resolved_home)
            return False

        stop_event = threading.Event()
        handle = _CollectorHandle(
            home=resolved_home,
            stop_event=stop_event,
            lock_fd=lock_fd,
            profile=profile,
            db_path=db_path,
        )

        def _worker():
            scope_token = None
            if profile:
                from hermes_constants import set_hermes_home_override
                scope_token = set_hermes_home_override(resolved_home)
            try:
                _run_collector_loop(
                    resolved_home=resolved_home,
                    stop_event=stop_event,
                    interval_seconds=interval_seconds,
                    profile=profile,
                    db_path=db_path,
                    base_url=base_url,
                    api_key=api_key,
                    max_retries=max_retries,
                )
            finally:
                if scope_token is not None:
                    from hermes_constants import reset_hermes_home_override
                    reset_hermes_home_override(scope_token)
                _release_collector_lease(lock_fd)
                handle.lock_fd = None
                with _collectors_lock:
                    if _active_collectors.get(resolved_home) is handle:
                        _active_collectors.pop(resolved_home, None)

        ctx = contextvars.copy_context()
        thread = threading.Thread(
            target=ctx.run,
            args=(_worker,),
            name=f"codex-quota-collector-{resolved_home.name}",
            daemon=True,
        )
        handle.thread = thread
        _active_collectors[resolved_home] = handle
        try:
            thread.start()
        except Exception:
            with _collectors_lock:
                if _active_collectors.get(resolved_home) is handle:
                    _active_collectors.pop(resolved_home, None)
            _release_collector_lease(lock_fd)
            handle.lock_fd = None
            raise
        return True


def stop_codex_quota_collector(
    profile: Optional[str] = None,
    *,
    db_path: Optional[Union[str, Path]] = None,
    timeout: float = 5.0,
) -> bool:
    """Stop the quota collector for the resolved home/profile.

    Returns:
        True if a running collector was stopped, False if none found or stop timed out.
    """
    target_home: Optional[Path] = None
    if profile is not None or db_path is not None:
        target_home = resolve_collector_home(profile=profile, db_path=db_path)

    with _collectors_lock:
        if not _active_collectors:
            return False

        handle = None
        if target_home is not None:
            handle = _active_collectors.get(target_home)
        else:
            current_home = resolve_collector_home()
            if current_home in _active_collectors:
                handle = _active_collectors.get(current_home)
            elif len(_active_collectors) == 1:
                handle = next(iter(_active_collectors.values()))

        if handle is None:
            return False

        if handle.thread is not None and not handle.thread.is_alive():
            if _active_collectors.get(handle.home) is handle:
                _active_collectors.pop(handle.home, None)
            return False

    handle.stop_event.set()
    if handle.thread is not None and handle.thread.is_alive():
        handle.thread.join(timeout=timeout)

    stopped = handle.thread is None or not handle.thread.is_alive()
    if stopped:
        with _collectors_lock:
            if _active_collectors.get(handle.home) is handle:
                _active_collectors.pop(handle.home, None)
    return stopped


def is_collector_running(
    profile: Optional[str] = None,
    *,
    db_path: Optional[Union[str, Path]] = None,
) -> bool:
    """Check whether a quota collector is active for the given profile/home."""
    target_home = resolve_collector_home(profile=profile, db_path=db_path)
    with _collectors_lock:
        handle = _active_collectors.get(target_home)
        if handle is not None:
            if handle.thread is not None and handle.thread.is_alive():
                return True
            if _active_collectors.get(target_home) is handle:
                _active_collectors.pop(target_home, None)
        return False


def _stop_all_collectors(timeout: float = 5.0) -> bool:
    """Stop all running quota collectors (useful for process exit and test teardown).

    Returns:
        True if all collectors stopped, False if any timed out.
    """
    with _collectors_lock:
        handles = list(_active_collectors.values())

    for handle in handles:
        handle.stop_event.set()

    all_stopped = True
    for handle in handles:
        if handle.thread is not None and handle.thread.is_alive():
            handle.thread.join(timeout=timeout)
            if handle.thread.is_alive():
                all_stopped = False

        if handle.thread is None or not handle.thread.is_alive():
            with _collectors_lock:
                if _active_collectors.get(handle.home) is handle:
                    _active_collectors.pop(handle.home, None)

    return all_stopped


def collect_once(
    *,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[CodexQuotaSnapshotRecord]:
    """Resolve current credentials once, fetch usage once, and persist observation.

    Refuses execution if account identity cannot be resolved (no database file touched).
    Does not attempt credential rotation or refresh on failure.
    Maps HTTP/network failures into stable error codes without leaking raw error messages.

    Binds both credential scope and database path when profile is provided.

    Returns:
        Stored CodexQuotaSnapshotRecord, or None if identity resolution failed.
    """
    home_token = None
    if profile:
        target_home = resolve_collector_home(profile=profile, db_path=db_path)
        from hermes_constants import set_hermes_home_override
        home_token = set_hermes_home_override(target_home)

    try:
        try:
            token, resolved_base_url, cred_account_id = _resolve_codex_usage_credentials(base_url, api_key)
        except Exception:
            logger.debug("Failed to resolve Codex usage credentials")
            return None

        if not token or not resolved_base_url:
            return None

        headers = _codex_headers(token, cred_account_id)
        account_id = cred_account_id or headers.get("ChatGPT-Account-ID")
        if not account_id:
            logger.debug("Refusing collection: missing Codex account identity")
            return None

        now_ts = time.time()
        usage_url = _codex_backend_urls(resolved_base_url)[0]

        try:
            payload = _get_json(usage_url, headers, timeout=15.0)
            if not isinstance(payload, dict):
                obs = {
                    "account_id": account_id,
                    "observed_at": now_ts,
                    "status": "error",
                    "error_code": "INVALID_RESPONSE",
                    "primary_window": None,
                    "secondary_window": None,
                }
                return record_codex_quota_snapshot(obs, db_path=db_path, profile=profile)

            rate_limit = payload.get("rate_limit") or {}
            primary_window = _map_window(rate_limit.get("primary_window"))
            secondary_window = _map_window(rate_limit.get("secondary_window"))

            obs = {
                "account_id": account_id,
                "observed_at": now_ts,
                "status": "ok",
                "error_code": None,
                "primary_window": primary_window,
                "secondary_window": secondary_window,
            }
            if payload.get("plan_type"):
                obs["plan_type"] = payload["plan_type"]

            snapshot = record_codex_quota_snapshot(obs, db_path=db_path, profile=profile)
            if snapshot is not None and snapshot.status == "ok":
                try:
                    from hermes_cli.codex_usage_attribution import capture_checkpoint
                    capture_checkpoint(
                        account_id=snapshot.account_id,
                        observed_at=snapshot.observed_at,
                        db_path=db_path,
                        profile=profile,
                        checkpoint_id=snapshot.observation_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to capture Codex activity checkpoint for %s (%s): %s",
                        snapshot.account_id,
                        snapshot.observation_id,
                        exc,
                    )
            return snapshot

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            error_code = f"HTTP_{status_code}"
            obs = {
                "account_id": account_id,
                "observed_at": now_ts,
                "status": "error",
                "error_code": error_code,
                "primary_window": None,
                "secondary_window": None,
            }
            return record_codex_quota_snapshot(obs, db_path=db_path, profile=profile)

        except httpx.TimeoutException:
            obs = {
                "account_id": account_id,
                "observed_at": now_ts,
                "status": "error",
                "error_code": "TIMEOUT",
                "primary_window": None,
                "secondary_window": None,
            }
            return record_codex_quota_snapshot(obs, db_path=db_path, profile=profile)

        except httpx.RequestError:
            obs = {
                "account_id": account_id,
                "observed_at": now_ts,
                "status": "error",
                "error_code": "NETWORK_ERROR",
                "primary_window": None,
                "secondary_window": None,
            }
            return record_codex_quota_snapshot(obs, db_path=db_path, profile=profile)

        except Exception:
            obs = {
                "account_id": account_id,
                "observed_at": now_ts,
                "status": "error",
                "error_code": "FETCH_ERROR",
                "primary_window": None,
                "secondary_window": None,
            }
            return record_codex_quota_snapshot(obs, db_path=db_path, profile=profile)

    finally:
        if home_token is not None:
            from hermes_constants import reset_hermes_home_override
            reset_hermes_home_override(home_token)
