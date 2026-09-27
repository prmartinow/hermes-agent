"""Codex Quota Snapshots & Interval Derivation (Storage-Only Unit).

Provides local SQLite persistence and interval derivation for allowlisted,
account-pinned Codex provider quota window observations.

Design Principles & Invariants:
1. Exact Timestamps & No Slot Bucketing:
   Unlike Gemini's 15-minute slot overwrites (which erase intermediate depletion
   and reset evidence), Codex observations preserve exact ``observed_at`` timestamps
   and window reset identifiers. Multiple samples in an interval are all preserved.
2. Allowlisted, Sanitized Storage:
   Only allowlisted fields are persisted: account_id, observed_at, window usages,
   reset identifiers, and collection error statuses.
   NEVER store tokens, secrets, authorization headers, raw provider JSON blobs,
   or billing credit balances.
3. Bounded Retention & Idempotency:
   Bounded retention (default 30 days) automatically prunes older samples.
   Idempotent storage: re-recording an observation with identical
   (account_id, observed_at) or (account_id, observation_id) updates existing state
   without duplicating rows.
4. Reset & Negative Delta Classification:
   Negative deltas or changes in ``reset_at`` represent discontinuities, resets,
   replenishments, or window rollovers. We NEVER assert that negative deltas are
   solely caused by local credit redemption; subscription windows reset naturally
   upon expiration, and resets/adjustments can occur externally (ChatGPT web, Codex CLI).
5. Strict Account & Window Isolation:
   Interval deltas are derived ONLY between consecutive ordered observations
   for the SAME account and the SAME window identity. Cross-account comparisons
   are strictly forbidden.
6. Attribution Invariant & Correction of Flawed Attribution:
   Summing cumulative ``session_model_usage`` at ``last_seen`` across an interval [T0, T1]
   is mathematically and architecturally INVALID because:
   a) ``session_model_usage`` records lifetime cumulative token counts per session,
      not per-interval deltas. Filtering by ``T0 <= last_seen <= T1`` attributes
      entire session lifetimes to that interval.
   b) Sessions active in [T0, T1] that execute further calls after T1 have their
      ``last_seen`` updated to > T1, omitting their interval usage entirely.
   c) Subscription limits are shared with external clients (Codex CLI, VS Code,
      ChatGPT web/mobile) and concurrent processes; synthetic proportional quota
      debit splits are physically ungrounded.
   Attribution in later phases MUST use a per-request event ledger or measured
   cumulative counter differences, NEVER local token proportional quota splits.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


# 30 days retention in seconds (30 * 86,400)
DEFAULT_RETENTION_SECONDS = 30 * 86400.0


@dataclass(frozen=True)
class CodexWindowObservation:
    """Sanitized observation for a specific quota window (e.g. primary 5h, secondary 7d)."""

    used_percent: Optional[float] = None
    reset_at: Optional[str] = None
    window_seconds: Optional[int] = None


@dataclass(frozen=True)
class CodexQuotaObservation:
    """Sanitized input payload representing an observation of Codex account quota."""

    account_id: str
    observed_at: float
    observation_id: Optional[str] = None
    status: str = "ok"  # 'ok', 'error', 'gap'
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    plan_type: Optional[str] = None
    primary_window: Optional[CodexWindowObservation] = None
    secondary_window: Optional[CodexWindowObservation] = None
    time_label: Optional[str] = None


@dataclass(frozen=True)
class CodexQuotaSnapshotRecord:
    """Persisted snapshot record in SQLite."""

    id: int
    observation_id: str
    observed_at: float
    time_label: str
    account_id: str
    status: str
    error_code: Optional[str]
    error_message: Optional[str]
    plan_type: Optional[str]
    primary_used_percent: Optional[float]
    primary_reset_at: Optional[str]
    primary_window_seconds: Optional[int]
    secondary_used_percent: Optional[float]
    secondary_reset_at: Optional[str]
    secondary_window_seconds: Optional[int]
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodexQuotaInterval:
    """Derived interval between two consecutive observations for the same account & window."""

    account_id: str
    window_id: str  # "primary" or "secondary"
    start_time: float
    end_time: float
    duration_seconds: float
    start_used_percent: Optional[float]
    end_used_percent: Optional[float]
    delta_used_percent: Optional[float]
    start_reset_at: Optional[str]
    end_reset_at: Optional[str]
    reset_at_changed: bool
    kind: str  # "depletion", "reset_or_replenishment", "gap", "discontinuity", "unchanged"
    status: str  # "ok", "gap", "discontinuity", "reset_or_replenishment"
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Validation & Sanitization Helpers
# ---------------------------------------------------------------------------

def _validate_str(val: Any, name: str, *, allow_none: bool = True) -> Optional[str]:
    if val is None:
        if allow_none:
            return None
        raise ValueError(f"{name} cannot be None")
    if not isinstance(val, str):
        raise ValueError(f"{name} must be a string, got {type(val).__name__}")
    s = val.strip()
    if not allow_none and not s:
        raise ValueError(f"{name} cannot be empty")
    return s


def _validate_number(
    val: Any,
    name: str,
    *,
    allow_none: bool = True,
    min_val: Optional[float] = None,
    max_val: Optional[float] = None,
) -> Optional[float]:
    if val is None:
        if allow_none:
            return None
        raise ValueError(f"{name} cannot be None")
    # Python bool is an instance of int; reject booleans explicitly
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be numeric, got {type(val).__name__}")
    f_val = float(val)
    if not math.isfinite(f_val):
        raise ValueError(f"{name} must be finite, got {f_val}")
    if min_val is not None and f_val < min_val:
        raise ValueError(f"{name} must be >= {min_val}, got {f_val}")
    if max_val is not None and f_val > max_val:
        raise ValueError(f"{name} must be <= {max_val}, got {f_val}")
    return f_val


def _validate_int(
    val: Any,
    name: str,
    *,
    allow_none: bool = True,
    min_val: Optional[int] = None,
) -> Optional[int]:
    if val is None:
        if allow_none:
            return None
        raise ValueError(f"{name} cannot be None")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be an integer, got {type(val).__name__}")
    if isinstance(val, float) and not val.is_integer():
        raise ValueError(f"{name} must be an integer, got {val}")
    i_val = int(val)
    if min_val is not None and i_val < min_val:
        raise ValueError(f"{name} must be >= {min_val}, got {i_val}")
    return i_val


def sanitize_codex_observation(
    raw: Union[CodexQuotaObservation, Dict[str, Any]],
) -> Dict[str, Any]:
    """Sanitize and allowlist incoming observation data.

    Discards sensitive fields (tokens, secrets, credit balances, raw blobs).
    Strictly validates types and value bounds.
    """
    if isinstance(raw, CodexQuotaObservation):
        account_id = _validate_str(raw.account_id, "account_id", allow_none=False)
        observed_at = _validate_number(raw.observed_at, "observed_at", allow_none=False, min_val=0.0)
        observation_id = _validate_str(raw.observation_id, "observation_id", allow_none=True)
        status = _validate_str(raw.status or "ok", "status", allow_none=False)
        error_code = _validate_str(raw.error_code, "error_code", allow_none=True)
        error_message = _validate_str(raw.error_message, "error_message", allow_none=True)
        plan_type = _validate_str(raw.plan_type, "plan_type", allow_none=True)
        time_label = _validate_str(raw.time_label, "time_label", allow_none=True)

        pw = raw.primary_window
        sw = raw.secondary_window

        p_used = _validate_number(pw.used_percent if pw else None, "primary_used_percent", allow_none=True, min_val=0.0, max_val=100.0)
        p_reset = _validate_str(str(pw.reset_at) if (pw and pw.reset_at is not None) else None, "primary_reset_at", allow_none=True)
        p_sec = _validate_int(pw.window_seconds if pw else None, "primary_window_seconds", allow_none=True, min_val=0)

        s_used = _validate_number(sw.used_percent if sw else None, "secondary_used_percent", allow_none=True, min_val=0.0, max_val=100.0)
        s_reset = _validate_str(str(sw.reset_at) if (sw and sw.reset_at is not None) else None, "secondary_reset_at", allow_none=True)
        s_sec = _validate_int(sw.window_seconds if sw else None, "secondary_window_seconds", allow_none=True, min_val=0)

    elif isinstance(raw, dict):
        account_id = _validate_str(raw.get("account_id"), "account_id", allow_none=False)
        observed_at = _validate_number(raw.get("observed_at"), "observed_at", allow_none=False, min_val=0.0)
        observation_id = _validate_str(raw.get("observation_id"), "observation_id", allow_none=True)
        status = _validate_str(raw.get("status") or "ok", "status", allow_none=False)
        error_code = _validate_str(raw.get("error_code"), "error_code", allow_none=True)
        error_message = _validate_str(raw.get("error_message"), "error_message", allow_none=True)
        plan_type = _validate_str(raw.get("plan_type"), "plan_type", allow_none=True)
        time_label = _validate_str(raw.get("time_label"), "time_label", allow_none=True)

        pw_raw = raw.get("primary_window")
        sw_raw = raw.get("secondary_window")

        if isinstance(pw_raw, (dict, CodexWindowObservation)):
            p_used_raw = getattr(pw_raw, "used_percent", None) if isinstance(pw_raw, CodexWindowObservation) else pw_raw.get("used_percent")
            p_reset_raw = getattr(pw_raw, "reset_at", None) if isinstance(pw_raw, CodexWindowObservation) else pw_raw.get("reset_at")
            p_sec_raw = getattr(pw_raw, "window_seconds", None) if isinstance(pw_raw, CodexWindowObservation) else pw_raw.get("window_seconds")
        else:
            p_used_raw = raw.get("primary_used_percent")
            p_reset_raw = raw.get("primary_reset_at")
            p_sec_raw = raw.get("primary_window_seconds")

        if isinstance(sw_raw, (dict, CodexWindowObservation)):
            s_used_raw = getattr(sw_raw, "used_percent", None) if isinstance(sw_raw, CodexWindowObservation) else sw_raw.get("used_percent")
            s_reset_raw = getattr(sw_raw, "reset_at", None) if isinstance(sw_raw, CodexWindowObservation) else sw_raw.get("reset_at")
            s_sec_raw = getattr(sw_raw, "window_seconds", None) if isinstance(sw_raw, CodexWindowObservation) else sw_raw.get("window_seconds")
        else:
            s_used_raw = raw.get("secondary_used_percent")
            s_reset_raw = raw.get("secondary_reset_at")
            s_sec_raw = raw.get("secondary_window_seconds")

        p_used = _validate_number(p_used_raw, "primary_used_percent", allow_none=True, min_val=0.0, max_val=100.0)
        p_reset = _validate_str(str(p_reset_raw) if p_reset_raw is not None else None, "primary_reset_at", allow_none=True)
        p_sec = _validate_int(p_sec_raw, "primary_window_seconds", allow_none=True, min_val=0)

        s_used = _validate_number(s_used_raw, "secondary_used_percent", allow_none=True, min_val=0.0, max_val=100.0)
        s_reset = _validate_str(str(s_reset_raw) if s_reset_raw is not None else None, "secondary_reset_at", allow_none=True)
        s_sec = _validate_int(s_sec_raw, "secondary_window_seconds", allow_none=True, min_val=0)
    else:
        raise ValueError(f"Observation must be dict or CodexQuotaObservation, got {type(raw).__name__}")

    # Generate deterministic observation ID if not supplied
    if not observation_id:
        obs_ms = int(observed_at * 1000)
        observation_id = f"obs_{account_id}_{obs_ms}"

    # Generate ISO time label if not provided
    if not time_label:
        time_label = datetime.fromtimestamp(observed_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return {
        "account_id": account_id,
        "observed_at": observed_at,
        "observation_id": observation_id,
        "time_label": time_label,
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
        "plan_type": plan_type,
        "primary_used_percent": p_used,
        "primary_reset_at": p_reset,
        "primary_window_seconds": p_sec,
        "secondary_used_percent": s_used,
        "secondary_reset_at": s_reset,
        "secondary_window_seconds": s_sec,
    }


# ---------------------------------------------------------------------------
# Profile-Aware Database Path Resolution
# ---------------------------------------------------------------------------

def get_codex_quota_db_path(
    *,
    profile: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Resolve the state.db path following existing Hermes profile conventions.

    Precedence:
    1. Explicit ``db_path`` parameter wins.
    2. Named ``profile`` (if not None/empty/default/current): resolves via
       ``hermes_cli.profiles.get_profile_dir(profile) / "state.db"``.
    3. Default: resolves via ``hermes_constants.get_hermes_home() / "state.db"``,
       which respects any active context-local override (set_hermes_home_override)
       or HERMES_HOME environment variable.
    """
    if db_path is not None:
        return Path(db_path)

    raw_profile = (profile or "").strip()
    if raw_profile and raw_profile.lower() not in ("default", "current"):
        try:
            from hermes_cli import profiles as profiles_mod
            canon = profiles_mod.normalize_profile_name(raw_profile)
            if canon != "default":
                return profiles_mod.get_profile_dir(canon) / "state.db"
        except Exception:
            # Fall back to home-based resolution if profiles module fails
            pass

    from hermes_constants import get_hermes_home
    return get_hermes_home() / "state.db"


def _connect_db(path: Path) -> sqlite3.Connection:
    """Open SQLite connection with appropriate timeout and WAL support."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _connect_ro_db(path: Union[str, Path]) -> Optional[sqlite3.Connection]:
    """Open a genuinely read-only SQLite connection.

    Uses URI filename with mode=ro. Does not create directories or files.
    Attaches a defensive SQLite authorizer denying mutating actions
    (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, ATTACH, DETACH).
    """
    target = Path(path)
    if not target.is_file():
        return None
    try:
        uri = target.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA query_only = ON")

        def _readonly_authorizer(action, _a1, _a2, _db, _tr):
            if action in (
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_CREATE_INDEX,
                sqlite3.SQLITE_CREATE_TEMP_TABLE,
                sqlite3.SQLITE_CREATE_TEMP_INDEX,
                sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
                sqlite3.SQLITE_CREATE_TEMP_VIEW,
                sqlite3.SQLITE_CREATE_TRIGGER,
                sqlite3.SQLITE_CREATE_VIEW,
                sqlite3.SQLITE_CREATE_VTABLE,
                sqlite3.SQLITE_DROP_INDEX,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_DROP_TEMP_INDEX,
                sqlite3.SQLITE_DROP_TEMP_TABLE,
                sqlite3.SQLITE_DROP_TEMP_TRIGGER,
                sqlite3.SQLITE_DROP_TEMP_VIEW,
                sqlite3.SQLITE_DROP_TRIGGER,
                sqlite3.SQLITE_DROP_VIEW,
                sqlite3.SQLITE_DROP_VTABLE,
                sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_ATTACH,
                sqlite3.SQLITE_DETACH,
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(_readonly_authorizer)
        return conn
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return None


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if table exists in database without triggering schema changes."""
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Table Initialization & Migrations
# ---------------------------------------------------------------------------

def init_codex_quota_snapshots_table(conn: sqlite3.Connection) -> None:
    """Initialize codex_quota_snapshots table and indexes in SQLite."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS codex_quota_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id TEXT NOT NULL,
            observed_at REAL NOT NULL,
            time_label TEXT NOT NULL,
            account_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ok',
            error_code TEXT,
            error_message TEXT,
            plan_type TEXT,
            primary_used_percent REAL,
            primary_reset_at TEXT,
            primary_window_seconds INTEGER,
            secondary_used_percent REAL,
            secondary_reset_at TEXT,
            secondary_window_seconds INTEGER,
            created_at REAL NOT NULL
        )
    """)

    # Ensure all columns exist for migration compatibility
    try:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(codex_quota_snapshots)").fetchall()}
        col_defs = [
            ("observation_id", "TEXT"),
            ("observed_at", "REAL"),
            ("time_label", "TEXT"),
            ("account_id", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'ok'"),
            ("error_code", "TEXT"),
            ("error_message", "TEXT"),
            ("plan_type", "TEXT"),
            ("primary_used_percent", "REAL"),
            ("primary_reset_at", "TEXT"),
            ("primary_window_seconds", "INTEGER"),
            ("secondary_used_percent", "REAL"),
            ("secondary_reset_at", "TEXT"),
            ("secondary_window_seconds", "INTEGER"),
            ("created_at", "REAL"),
        ]
        for col_name, col_type in col_defs:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE codex_quota_snapshots ADD COLUMN {col_name} {col_type}")
    except Exception:
        pass

    # Unique index on (account_id, observed_at) ensures no duplicate observations for an account at the exact same instant
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_codex_quota_snapshots_acc_obs
        ON codex_quota_snapshots (account_id, observed_at)
    """)

    # Unique index on (account_id, observation_id) for idempotent ingestion
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_codex_quota_snapshots_acc_obs_id
        ON codex_quota_snapshots (account_id, observation_id)
    """)

    # Fast range and ordering index
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_codex_quota_snapshots_acc_time
        ON codex_quota_snapshots (account_id, observed_at)
    """)


# ---------------------------------------------------------------------------
# Storage & Record Ingestion API
# ---------------------------------------------------------------------------

def record_codex_quota_snapshot(
    observation: Union[CodexQuotaObservation, Dict[str, Any]],
    *,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
    retention_days: int = 30,
) -> CodexQuotaSnapshotRecord:
    """Record an allowlisted, sanitized Codex quota observation into SQLite.

    Idempotent:
    If a record with matching ``(account_id, observation_id)`` or matching
    ``(account_id, observed_at)`` already exists, it is updated in place
    rather than creating a duplicate row.

    Bounded Retention:
    Automatically deletes records older than ``observed_at - (retention_days * 86400)``.
    """
    clean = sanitize_codex_observation(observation)
    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    now_epoch = time.time()

    conn = _connect_db(target_path)
    try:
        with conn:
            init_codex_quota_snapshots_table(conn)

            # Check for existing record by observation_id OR (account_id, observed_at)
            cur = conn.execute(
                """
                SELECT id FROM codex_quota_snapshots
                WHERE (account_id = ? AND observation_id = ?)
                   OR (account_id = ? AND observed_at = ?)
                LIMIT 1
                """,
                (clean["account_id"], clean["observation_id"], clean["account_id"], clean["observed_at"]),
            )
            existing = cur.fetchone()

            if existing is not None:
                record_id = existing[0]
                conn.execute(
                    """
                    UPDATE codex_quota_snapshots SET
                        observation_id = ?,
                        observed_at = ?,
                        time_label = ?,
                        account_id = ?,
                        status = ?,
                        error_code = ?,
                        error_message = ?,
                        plan_type = ?,
                        primary_used_percent = ?,
                        primary_reset_at = ?,
                        primary_window_seconds = ?,
                        secondary_used_percent = ?,
                        secondary_reset_at = ?,
                        secondary_window_seconds = ?,
                        created_at = ?
                    WHERE id = ?
                    """,
                    (
                        clean["observation_id"],
                        clean["observed_at"],
                        clean["time_label"],
                        clean["account_id"],
                        clean["status"],
                        clean["error_code"],
                        clean["error_message"],
                        clean["plan_type"],
                        clean["primary_used_percent"],
                        clean["primary_reset_at"],
                        clean["primary_window_seconds"],
                        clean["secondary_used_percent"],
                        clean["secondary_reset_at"],
                        clean["secondary_window_seconds"],
                        now_epoch,
                        record_id,
                    ),
                )
            else:
                insert_cur = conn.execute(
                    """
                    INSERT INTO codex_quota_snapshots (
                        observation_id, observed_at, time_label, account_id,
                        status, error_code, error_message, plan_type,
                        primary_used_percent, primary_reset_at, primary_window_seconds,
                        secondary_used_percent, secondary_reset_at, secondary_window_seconds,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean["observation_id"],
                        clean["observed_at"],
                        clean["time_label"],
                        clean["account_id"],
                        clean["status"],
                        clean["error_code"],
                        clean["error_message"],
                        clean["plan_type"],
                        clean["primary_used_percent"],
                        clean["primary_reset_at"],
                        clean["primary_window_seconds"],
                        clean["secondary_used_percent"],
                        clean["secondary_reset_at"],
                        clean["secondary_window_seconds"],
                        now_epoch,
                    ),
                )
                record_id = insert_cur.lastrowid

            # Bounded retention pruning
            if retention_days > 0:
                cutoff = clean["observed_at"] - (float(retention_days) * 86400.0)
                conn.execute(
                    "DELETE FROM codex_quota_snapshots WHERE observed_at < ?",
                    (cutoff,),
                )

        return CodexQuotaSnapshotRecord(
            id=record_id,
            observation_id=clean["observation_id"],
            observed_at=clean["observed_at"],
            time_label=clean["time_label"],
            account_id=clean["account_id"],
            status=clean["status"],
            error_code=clean["error_code"],
            error_message=clean["error_message"],
            plan_type=clean["plan_type"],
            primary_used_percent=clean["primary_used_percent"],
            primary_reset_at=clean["primary_reset_at"],
            primary_window_seconds=clean["primary_window_seconds"],
            secondary_used_percent=clean["secondary_used_percent"],
            secondary_reset_at=clean["secondary_reset_at"],
            secondary_window_seconds=clean["secondary_window_seconds"],
            created_at=now_epoch,
        )
    finally:
        conn.close()


def prune_codex_quota_snapshots(
    *,
    retention_days: int = 30,
    before_timestamp: Optional[float] = None,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
) -> int:
    """Explicitly prune records older than the retention threshold.

    Returns the count of pruned rows.
    """
    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    if not target_path.exists():
        return 0

    if before_timestamp is not None:
        cutoff = float(before_timestamp)
    else:
        cutoff = time.time() - (float(retention_days) * 86400.0)

    conn = _connect_db(target_path)
    try:
        if not _table_exists(conn, "codex_quota_snapshots"):
            return 0
        with conn:
            cur = conn.execute("DELETE FROM codex_quota_snapshots WHERE observed_at < ?", (cutoff,))
            return cur.rowcount
    finally:
        conn.close()


def list_codex_quota_snapshots(
    *,
    account_id: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: Optional[int] = None,
    order_desc: bool = False,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
) -> List[CodexQuotaSnapshotRecord]:
    """Retrieve stored quota snapshots matching filters, ordered chronologically.

    Genuinely read-only: uses URI mode=ro, does not initialize schema or modify tables.
    If database or table does not exist, returns empty list.
    """
    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    conn = _connect_ro_db(target_path)
    if conn is None:
        return []

    try:
        if not _table_exists(conn, "codex_quota_snapshots"):
            return []

        query_parts = ["SELECT * FROM codex_quota_snapshots"]
        where_clauses: List[str] = []
        params: List[Any] = []

        if account_id:
            where_clauses.append("account_id = ?")
            params.append(account_id.strip())
        if since is not None:
            where_clauses.append("observed_at >= ?")
            params.append(float(since))
        if until is not None:
            where_clauses.append("observed_at <= ?")
            params.append(float(until))

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        direction = "DESC" if order_desc else "ASC"
        query_parts.append(f"ORDER BY observed_at {direction}, id {direction}")

        if limit is not None and limit > 0:
            query_parts.append("LIMIT ?")
            params.append(int(limit))

        sql = " ".join(query_parts)
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [
            CodexQuotaSnapshotRecord(
                id=r["id"],
                observation_id=r["observation_id"],
                observed_at=r["observed_at"],
                time_label=r["time_label"],
                account_id=r["account_id"],
                status=r["status"],
                error_code=r["error_code"],
                error_message=r["error_message"],
                plan_type=r["plan_type"],
                primary_used_percent=r["primary_used_percent"],
                primary_reset_at=r["primary_reset_at"],
                primary_window_seconds=r["primary_window_seconds"],
                secondary_used_percent=r["secondary_used_percent"],
                secondary_reset_at=r["secondary_reset_at"],
                secondary_window_seconds=r["secondary_window_seconds"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Interval Derivation Logic
# ---------------------------------------------------------------------------

def _normalize_window_id(window_id: str) -> str:
    """Normalize window identifier to 'primary' or 'secondary'."""
    raw = (window_id or "primary").strip().lower()
    if raw in ("primary", "session", "5h", "primary_window"):
        return "primary"
    if raw in ("secondary", "weekly", "7d", "secondary_window"):
        return "secondary"
    raise ValueError(f"Unrecognized window_id: {window_id!r}. Expected 'primary' or 'secondary'.")


def derive_codex_quota_intervals(
    snapshots_or_records: Optional[Sequence[Union[CodexQuotaSnapshotRecord, Dict[str, Any]]]] = None,
    *,
    account_id: Optional[str] = None,
    window_id: str = "primary",
    since: Optional[float] = None,
    until: Optional[float] = None,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
) -> List[CodexQuotaInterval]:
    """Derive sequential quota usage intervals from stored or provided snapshots.

    Invariants:
    1. Isolation: Compares ONLY observations sharing the SAME account_id and
       the SAME window identity. Cross-account comparisons are never made.
    2. Ordered Timestamps: Evaluates observations ordered chronologically (observed_at ASC).
    3. Negative Delta / reset_at Change: Classified as ``reset_or_replenishment``
       or ``discontinuity``; does NOT assert a redeemed reset credit.
    4. Gaps & Missing Values: Missing usage or error statuses are flagged as ``gap``
       with ``delta_used_percent = None`` without corrupting adjacent intervals.
    """
    canon_window = _normalize_window_id(window_id)

    # If snapshots not provided, query from DB
    if snapshots_or_records is None:
        records = list_codex_quota_snapshots(
            account_id=account_id,
            since=since,
            until=until,
            order_desc=False,
            db_path=db_path,
            profile=profile,
        )
    else:
        # Convert any dicts to record objects
        parsed_records: List[CodexQuotaSnapshotRecord] = []
        for item in snapshots_or_records:
            if isinstance(item, CodexQuotaSnapshotRecord):
                parsed_records.append(item)
            elif isinstance(item, dict):
                clean = sanitize_codex_observation(item)
                parsed_records.append(
                    CodexQuotaSnapshotRecord(
                        id=item.get("id") or 0,
                        observation_id=clean["observation_id"],
                        observed_at=clean["observed_at"],
                        time_label=clean["time_label"],
                        account_id=clean["account_id"],
                        status=clean["status"],
                        error_code=clean["error_code"],
                        error_message=clean["error_message"],
                        plan_type=clean["plan_type"],
                        primary_used_percent=clean["primary_used_percent"],
                        primary_reset_at=clean["primary_reset_at"],
                        primary_window_seconds=clean["primary_window_seconds"],
                        secondary_used_percent=clean["secondary_used_percent"],
                        secondary_reset_at=clean["secondary_reset_at"],
                        secondary_window_seconds=clean["secondary_window_seconds"],
                        created_at=item.get("created_at") or time.time(),
                    )
                )
            else:
                raise ValueError(f"Expected CodexQuotaSnapshotRecord or dict, got {type(item).__name__}")

        records = parsed_records

        # Apply in-memory filters
        if account_id:
            records = [r for r in records if r.account_id == account_id.strip()]
        if since is not None:
            records = [r for r in records if r.observed_at >= float(since)]
        if until is not None:
            records = [r for r in records if r.observed_at <= float(until)]

    # Group strictly by account_id so we NEVER compare across accounts
    by_account: Dict[str, List[CodexQuotaSnapshotRecord]] = {}
    for r in records:
        by_account.setdefault(r.account_id, []).append(r)

    derived_intervals: List[CodexQuotaInterval] = []

    for acc, acc_records in by_account.items():
        # Sort chronologically
        sorted_records = sorted(acc_records, key=lambda x: (x.observed_at, x.id))
        if len(sorted_records) < 2:
            continue

        for i in range(len(sorted_records) - 1):
            prev = sorted_records[i]
            curr = sorted_records[i + 1]

            t0 = prev.observed_at
            t1 = curr.observed_at
            duration = max(0.0, t1 - t0)

            if canon_window == "primary":
                u0 = prev.primary_used_percent
                u1 = curr.primary_used_percent
                r0 = prev.primary_reset_at
                r1 = curr.primary_reset_at
            else:
                u0 = prev.secondary_used_percent
                u1 = curr.secondary_used_percent
                r0 = prev.secondary_reset_at
                r1 = curr.secondary_reset_at

            # Check for collection errors or gaps
            if prev.status != "ok" or curr.status != "ok" or u0 is None or u1 is None:
                reset_changed = (r0 != r1) if (r0 is not None and r1 is not None) else False
                err_code = curr.error_code or prev.error_code or ("missing_usage" if (u0 is None or u1 is None) else "gap")
                derived_intervals.append(
                    CodexQuotaInterval(
                        account_id=acc,
                        window_id=canon_window,
                        start_time=t0,
                        end_time=t1,
                        duration_seconds=duration,
                        start_used_percent=u0,
                        end_used_percent=u1,
                        delta_used_percent=None,
                        start_reset_at=r0,
                        end_reset_at=r1,
                        reset_at_changed=reset_changed,
                        kind="gap",
                        status="gap",
                        description=f"Observation gap or collection error ({err_code})",
                    )
                )
                continue

            # Both observations are 'ok' and numeric
            raw_delta = round(u1 - u0, 4)
            reset_changed = (r0 != r1) if (r0 is not None and r1 is not None) else False

            if reset_changed:
                # Window reset identifier shifted: reset or replenishment occurred
                derived_intervals.append(
                    CodexQuotaInterval(
                        account_id=acc,
                        window_id=canon_window,
                        start_time=t0,
                        end_time=t1,
                        duration_seconds=duration,
                        start_used_percent=u0,
                        end_used_percent=u1,
                        delta_used_percent=raw_delta,
                        start_reset_at=r0,
                        end_reset_at=r1,
                        reset_at_changed=True,
                        kind="reset_or_replenishment",
                        status="reset_or_replenishment",
                        description=(
                            f"Window reset identifier changed from {r0} to {r1}; "
                            f"possible reset, replenishment, or window rollover (delta: {raw_delta}%)"
                        ),
                    )
                )
            elif raw_delta < 0:
                # Negative delta without reset_at change: discontinuity or replenishment
                derived_intervals.append(
                    CodexQuotaInterval(
                        account_id=acc,
                        window_id=canon_window,
                        start_time=t0,
                        end_time=t1,
                        duration_seconds=duration,
                        start_used_percent=u0,
                        end_used_percent=u1,
                        delta_used_percent=raw_delta,
                        start_reset_at=r0,
                        end_reset_at=r1,
                        reset_at_changed=False,
                        kind="reset_or_replenishment",
                        status="discontinuity",
                        description=(
                            f"Negative quota delta ({u0}% -> {u1}%, delta: {raw_delta}%) "
                            f"without reset_at change; discontinuity or backend replenishment"
                        ),
                    )
                )
            elif raw_delta == 0:
                # No usage change
                derived_intervals.append(
                    CodexQuotaInterval(
                        account_id=acc,
                        window_id=canon_window,
                        start_time=t0,
                        end_time=t1,
                        duration_seconds=duration,
                        start_used_percent=u0,
                        end_used_percent=u1,
                        delta_used_percent=0.0,
                        start_reset_at=r0,
                        end_reset_at=r1,
                        reset_at_changed=False,
                        kind="unchanged",
                        status="ok",
                        description="Quota usage unchanged over interval",
                    )
                )
            else:
                # Normal positive depletion
                derived_intervals.append(
                    CodexQuotaInterval(
                        account_id=acc,
                        window_id=canon_window,
                        start_time=t0,
                        end_time=t1,
                        duration_seconds=duration,
                        start_used_percent=u0,
                        end_used_percent=u1,
                        delta_used_percent=raw_delta,
                        start_reset_at=r0,
                        end_reset_at=r1,
                        reset_at_changed=False,
                        kind="depletion",
                        status="ok",
                        description=f"Normal quota depletion of {raw_delta}%",
                    )
                )

    # Sort final intervals by start_time
    derived_intervals.sort(key=lambda x: (x.start_time, x.account_id))
    return derived_intervals
