"""Codex usage attribution via activity checkpoint storage.

Provides small, bounded activity checkpoint storage that captures cumulative
session_model_usage counters for openai-codex billing providers and derives
exact counter differences between observation endpoints.

Key properties:
- Explicit missing baseline and counter decrease (discontinuity) handling.
- Does NOT sum lifetime usage by last_seen.
- No per-session quota percentages (correlated local activity only; external
  concurrent usage is always possible).
- Bounded 30-day retention pruning.
- Keyed by (checkpoint_id, account_id, session_id, model, task).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from hermes_cli.codex_quota_snapshots import (
    _connect_ro_db,
    _table_exists,
    get_codex_quota_db_path,
)

DEFAULT_RETENTION_DAYS = 30
OPENAI_CODEX_PROVIDER = "openai-codex"


@dataclass(frozen=True)
class ActivityCounters:
    """Cumulative or delta activity counters."""

    api_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def cache_tokens(self) -> int:
        return self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> Dict[str, int]:
        return {
            "api_call_count": self.api_call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_tokens": self.cache_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class CodexActivityCheckpoint:
    """Snapshot of cumulative openai-codex usage counters at an observation instant."""

    checkpoint_id: str
    account_id: str
    observed_at: float
    created_at: float
    entries: Dict[Tuple[str, str, str], ActivityCounters] = field(default_factory=dict)

    def get_counters(self, session_id: str, model: str, task: str = "") -> Optional[ActivityCounters]:
        return self.entries.get((session_id, model, task))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "account_id": self.account_id,
            "observed_at": self.observed_at,
            "created_at": self.created_at,
            "entries": [
                {
                    "session_id": s_id,
                    "model": m,
                    "task": t,
                    **counters.to_dict(),
                }
                for (s_id, m, t), counters in sorted(self.entries.items())
            ],
        }


@dataclass
class SessionActivityDelta:
    """Attributed delta for a specific (session_id, model, task) between two checkpoints."""

    session_id: str
    model: str
    task: str
    status: str  # "ok", "missing_baseline", "counter_decrease", "missing_in_current"
    is_discontinuity: bool
    has_baseline: bool
    baseline_counters: Optional[ActivityCounters] = None
    current_counters: Optional[ActivityCounters] = None
    delta_counters: Optional[ActivityCounters] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "task": self.task,
            "status": self.status,
            "is_discontinuity": self.is_discontinuity,
            "has_baseline": self.has_baseline,
            "baseline_counters": self.baseline_counters.to_dict() if self.baseline_counters else None,
            "current_counters": self.current_counters.to_dict() if self.current_counters else None,
            "delta_counters": self.delta_counters.to_dict() if self.delta_counters else None,
            "reason": self.reason,
        }


@dataclass
class CheckpointDifferenceResult:
    """Result of deriving usage differences between two checkpoint endpoints of the same account."""

    account_id: str
    start_checkpoint_id: Optional[str]
    end_checkpoint_id: str
    start_observed_at: Optional[float]
    end_observed_at: float
    status: str  # "ok", "missing_baseline", "discontinuity"
    has_baseline: bool
    has_discontinuity: bool
    session_deltas: List[SessionActivityDelta] = field(default_factory=list)
    total_delta: Optional[ActivityCounters] = None
    known_delta: ActivityCounters = field(default_factory=ActivityCounters)
    discontinuity_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "start_checkpoint_id": self.start_checkpoint_id,
            "end_checkpoint_id": self.end_checkpoint_id,
            "start_observed_at": self.start_observed_at,
            "end_observed_at": self.end_observed_at,
            "status": self.status,
            "has_baseline": self.has_baseline,
            "has_discontinuity": self.has_discontinuity,
            "session_deltas": [d.to_dict() for d in self.session_deltas],
            "total_delta": self.total_delta.to_dict() if self.total_delta else None,
            "known_delta": self.known_delta.to_dict(),
            "discontinuity_reasons": self.discontinuity_reasons,
        }


def _connect_db(path: Path) -> sqlite3.Connection:
    """Open SQLite connection with appropriate timeout and WAL support."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_codex_activity_checkpoints_table(conn: sqlite3.Connection) -> None:
    """Initialize codex_activity_checkpoints table and indexes in SQLite."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS codex_activity_checkpoints (
            checkpoint_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            task TEXT NOT NULL DEFAULT '',
            api_call_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            observed_at REAL NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (checkpoint_id, account_id, session_id, model, task)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_codex_activity_checkpoints_acc_obs
        ON codex_activity_checkpoints (account_id, observed_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_codex_activity_checkpoints_chk
        ON codex_activity_checkpoints (checkpoint_id)
    """)


def prune_activity_checkpoints(
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
    ref_time: Optional[float] = None,
) -> int:
    """Prune activity checkpoints older than retention_days relative to ref_time."""
    if retention_days <= 0:
        return 0

    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    now_epoch = time.time() if ref_time is None else float(ref_time)
    cutoff = now_epoch - (float(retention_days) * 86400.0)

    conn = _connect_db(target_path)
    try:
        if not _table_exists(conn, "codex_activity_checkpoints"):
            return 0
        with conn:
            cur = conn.execute(
                "DELETE FROM codex_activity_checkpoints WHERE observed_at < ?",
                (cutoff,),
            )
            return cur.rowcount
    finally:
        conn.close()


def _read_openai_codex_usage(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Query cumulative usage from session_model_usage for openai-codex only."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_model_usage'"
    )
    if cur.fetchone() is None:
        return []

    cur = conn.execute("""
        SELECT
            session_id,
            model,
            COALESCE(task, '') AS task,
            COALESCE(SUM(api_call_count), 0) AS api_call_count,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
        FROM session_model_usage
        WHERE LOWER(billing_provider) = ?
        GROUP BY session_id, model, task
    """, (OPENAI_CODEX_PROVIDER.lower(),))

    return [dict(row) for row in cur.fetchall()]


def capture_checkpoint(
    account_id: str,
    observed_at: float,
    db_path: Optional[Union[str, Path]] = None,
    *,
    profile: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> CodexActivityCheckpoint:
    """Capture an activity checkpoint copying cumulative openai-codex counters.

    Copies only openai-codex model/session/task cumulative API call + input/cache/output
    counters into codex_activity_checkpoints table keyed by
    (checkpoint_id, account_id, session_id, model, task).

    Applies bounded retention pruning (default 30 days).
    """
    if not account_id or not isinstance(account_id, str):
        raise ValueError("account_id must be a non-empty string")
    clean_account_id = account_id.strip()
    if not clean_account_id:
        raise ValueError("account_id cannot be blank")

    obs_at = float(observed_at)
    now_epoch = time.time()
    cid = checkpoint_id or f"chk_{int(obs_at)}_{uuid.uuid4().hex[:12]}"

    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    conn = _connect_db(target_path)

    entries: Dict[Tuple[str, str, str], ActivityCounters] = {}
    try:
        with conn:
            init_codex_activity_checkpoints_table(conn)
            usage_rows = _read_openai_codex_usage(conn)

            if usage_rows:
                for row in usage_rows:
                    s_id = str(row["session_id"])
                    m = str(row["model"])
                    t = str(row.get("task") or "")
                    api_calls = int(row.get("api_call_count") or 0)
                    inp = int(row.get("input_tokens") or 0)
                    out = int(row.get("output_tokens") or 0)
                    c_read = int(row.get("cache_read_tokens") or 0)
                    c_write = int(row.get("cache_write_tokens") or 0)

                    counters = ActivityCounters(
                        api_call_count=api_calls,
                        input_tokens=inp,
                        output_tokens=out,
                        cache_read_tokens=c_read,
                        cache_write_tokens=c_write,
                    )
                    entries[(s_id, m, t)] = counters

                    conn.execute("""
                        INSERT OR REPLACE INTO codex_activity_checkpoints (
                            checkpoint_id, account_id, session_id, model, task,
                            api_call_count, input_tokens, output_tokens,
                            cache_read_tokens, cache_write_tokens,
                            observed_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        cid, clean_account_id, s_id, m, t,
                        api_calls, inp, out, c_read, c_write,
                        obs_at, now_epoch,
                    ))
            else:
                # Store a marker row with empty session/model/task so checkpoint existence is preserved
                conn.execute("""
                    INSERT OR REPLACE INTO codex_activity_checkpoints (
                        checkpoint_id, account_id, session_id, model, task,
                        api_call_count, input_tokens, output_tokens,
                        cache_read_tokens, cache_write_tokens,
                        observed_at, created_at
                    ) VALUES (?, ?, '', '', '', 0, 0, 0, 0, 0, ?, ?)
                """, (cid, clean_account_id, obs_at, now_epoch))

            # Bounded retention pruning
            if retention_days > 0:
                cutoff = obs_at - (float(retention_days) * 86400.0)
                conn.execute(
                    "DELETE FROM codex_activity_checkpoints WHERE observed_at < ?",
                    (cutoff,),
                )
    finally:
        conn.close()

    return CodexActivityCheckpoint(
        checkpoint_id=cid,
        account_id=clean_account_id,
        observed_at=obs_at,
        created_at=now_epoch,
        entries=entries,
    )


def get_checkpoint(
    checkpoint_id: str,
    *,
    account_id: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
) -> Optional[CodexActivityCheckpoint]:
    """Retrieve an activity checkpoint by checkpoint_id.

    Genuinely read-only: uses URI mode=ro, does not initialize schema or modify tables.
    If database or table does not exist, returns None.
    """
    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    conn = _connect_ro_db(target_path)
    if conn is None:
        return None

    try:
        if not _table_exists(conn, "codex_activity_checkpoints"):
            return None
        sql = "SELECT * FROM codex_activity_checkpoints WHERE checkpoint_id = ?"
        params: List[Any] = [checkpoint_id]
        if account_id:
            sql += " AND account_id = ?"
            params.append(account_id.strip())

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return None

        first = rows[0]
        acc_id = first["account_id"]
        obs_at = float(first["observed_at"])
        created_at = float(first["created_at"])

        entries: Dict[Tuple[str, str, str], ActivityCounters] = {}
        for row in rows:
            s_id = row["session_id"]
            if not s_id:
                # Skip checkpoint marker row
                continue
            m = row["model"]
            t = row["task"] or ""
            entries[(s_id, m, t)] = ActivityCounters(
                api_call_count=int(row["api_call_count"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cache_read_tokens=int(row["cache_read_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
            )

        return CodexActivityCheckpoint(
            checkpoint_id=checkpoint_id,
            account_id=acc_id,
            observed_at=obs_at,
            created_at=created_at,
            entries=entries,
        )
    finally:
        conn.close()


def get_latest_checkpoint(
    account_id: str,
    *,
    before_time: Optional[float] = None,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
) -> Optional[CodexActivityCheckpoint]:
    """Retrieve the most recent activity checkpoint for an account.

    Genuinely read-only: uses URI mode=ro, does not initialize schema or modify tables.
    If database or table does not exist, returns None.
    """
    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    conn = _connect_ro_db(target_path)
    if conn is None:
        return None

    cid = None
    try:
        if not _table_exists(conn, "codex_activity_checkpoints"):
            return None

        sql = """
            SELECT checkpoint_id
            FROM codex_activity_checkpoints
            WHERE account_id = ?
        """
        params: List[Any] = [account_id.strip()]
        if before_time is not None:
            sql += " AND observed_at < ?"
            params.append(float(before_time))
        sql += " ORDER BY observed_at DESC, created_at DESC LIMIT 1"

        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None

        cid = row["checkpoint_id"]
    finally:
        conn.close()

    return get_checkpoint(
        cid,
        account_id=account_id,
        db_path=target_path,
        profile=profile,
    )


def list_checkpoints(
    account_id: str,
    *,
    limit: int = 50,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
) -> List[CodexActivityCheckpoint]:
    """List activity checkpoints for an account ordered newest first.

    Genuinely read-only: uses URI mode=ro, does not initialize schema or modify tables.
    If database or table does not exist, returns empty list.
    """
    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    conn = _connect_ro_db(target_path)
    if conn is None:
        return []

    cids = []
    try:
        if not _table_exists(conn, "codex_activity_checkpoints"):
            return []

        rows = conn.execute("""
            SELECT DISTINCT checkpoint_id, observed_at
            FROM codex_activity_checkpoints
            WHERE account_id = ?
            ORDER BY observed_at DESC
            LIMIT ?
        """, (account_id.strip(), limit)).fetchall()

        cids = [r["checkpoint_id"] for r in rows]
    finally:
        conn.close()

    results: List[CodexActivityCheckpoint] = []
    for cid in cids:
        chk = get_checkpoint(
            cid,
            account_id=account_id,
            db_path=target_path,
            profile=profile,
        )
        if chk:
            results.append(chk)
    return results


def _resolve_checkpoint_endpoint(
    endpoint: Optional[Union[CodexActivityCheckpoint, str, Dict[str, Any]]],
    *,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
) -> Optional[CodexActivityCheckpoint]:
    """Resolve an endpoint parameter into a CodexActivityCheckpoint object."""
    if endpoint is None:
        return None
    if isinstance(endpoint, CodexActivityCheckpoint):
        return endpoint
    if isinstance(endpoint, str):
        chk = get_checkpoint(endpoint, db_path=db_path, profile=profile)
        if chk is None:
            raise ValueError(f"Checkpoint not found for id: {endpoint}")
        return chk
    if isinstance(endpoint, dict):
        cid = str(endpoint["checkpoint_id"])
        acc_id = str(endpoint["account_id"])
        obs_at = float(endpoint["observed_at"])
        created_at = float(endpoint.get("created_at") or obs_at)
        entries: Dict[Tuple[str, str, str], ActivityCounters] = {}
        for item in endpoint.get("entries", []):
            s_id = str(item["session_id"])
            if not s_id:
                continue
            m = str(item["model"])
            t = str(item.get("task") or "")
            entries[(s_id, m, t)] = ActivityCounters(
                api_call_count=int(item.get("api_call_count") or 0),
                input_tokens=int(item.get("input_tokens") or 0),
                output_tokens=int(item.get("output_tokens") or 0),
                cache_read_tokens=int(item.get("cache_read_tokens") or 0),
                cache_write_tokens=int(item.get("cache_write_tokens") or 0),
            )
        return CodexActivityCheckpoint(
            checkpoint_id=cid,
            account_id=acc_id,
            observed_at=obs_at,
            created_at=created_at,
            entries=entries,
        )
    raise TypeError(f"Unsupported endpoint type: {type(endpoint)}")


def derive_checkpoint_differences(
    start_endpoint: Optional[Union[CodexActivityCheckpoint, str, Dict[str, Any]]],
    end_endpoint: Union[CodexActivityCheckpoint, str, Dict[str, Any]],
    *,
    db_path: Optional[Union[str, Path]] = None,
    profile: Optional[str] = None,
) -> CheckpointDifferenceResult:
    """Derive activity counter differences between same-account checkpoint endpoints.

    Behavior:
    - Same Account Enforcement: Raises ValueError if start and end accounts differ.
    - Missing Baselines: If start_endpoint is None or a session/model/task is missing
      from the start checkpoint, it is explicitly flagged as unknown/missing_baseline/discontinuity.
      Lifetime counters are NEVER summed by last_seen.
    - Counter Decreases / Resets: If an end counter is lower than the start counter,
      it is explicitly flagged as discontinuity/counter_decrease rather than producing a negative delta.
    - No per-session quota percentages are computed (correlated local activity only).
    """
    end_chk = _resolve_checkpoint_endpoint(end_endpoint, db_path=db_path, profile=profile)
    if end_chk is None:
        raise ValueError("end_endpoint is required and could not be resolved")

    start_chk = _resolve_checkpoint_endpoint(start_endpoint, db_path=db_path, profile=profile)

    # Enforce same-account isolation
    if start_chk is not None and start_chk.account_id != end_chk.account_id:
        raise ValueError(
            f"Account mismatch between checkpoint endpoints: "
            f"start account '{start_chk.account_id}' != end account '{end_chk.account_id}'"
        )

    account_id = end_chk.account_id

    # Handle Case 1: Entire baseline checkpoint is missing
    if start_chk is None:
        session_deltas: List[SessionActivityDelta] = []
        for (s_id, m, t), curr_counters in sorted(end_chk.entries.items()):
            session_deltas.append(
                SessionActivityDelta(
                    session_id=s_id,
                    model=m,
                    task=t,
                    status="missing_baseline",
                    is_discontinuity=True,
                    has_baseline=False,
                    baseline_counters=None,
                    current_counters=curr_counters,
                    delta_counters=None,  # Explicitly unknown, not lifetime sum
                    reason="Missing baseline checkpoint for account",
                )
            )

        return CheckpointDifferenceResult(
            account_id=account_id,
            start_checkpoint_id=None,
            end_checkpoint_id=end_chk.checkpoint_id,
            start_observed_at=None,
            end_observed_at=end_chk.observed_at,
            status="missing_baseline",
            has_baseline=False,
            has_discontinuity=True,
            session_deltas=session_deltas,
            total_delta=None,  # Cannot sum lifetime by last_seen
            known_delta=ActivityCounters(),
            discontinuity_reasons=["Missing baseline checkpoint for account"],
        )

    # Handle Case 2: Both endpoints present for the same account
    all_keys = set(start_chk.entries.keys()) | set(end_chk.entries.keys())
    session_deltas: List[SessionActivityDelta] = []
    discontinuity_reasons: List[str] = []

    clean_deltas: List[ActivityCounters] = []

    for key in sorted(all_keys):
        s_id, m, t = key
        in_start = key in start_chk.entries
        in_end = key in end_chk.entries

        if in_end and not in_start:
            # Session appeared in end but was absent in start: missing baseline for this session
            curr_c = end_chk.entries[key]
            reason = f"Session/model/task ({s_id}, {m}, {t}) has no baseline in start checkpoint"
            discontinuity_reasons.append(reason)
            session_deltas.append(
                SessionActivityDelta(
                    session_id=s_id,
                    model=m,
                    task=t,
                    status="missing_baseline",
                    is_discontinuity=True,
                    has_baseline=False,
                    baseline_counters=None,
                    current_counters=curr_c,
                    delta_counters=None,  # Do NOT treat as 0 -> lifetime sum
                    reason=reason,
                )
            )

        elif in_start and not in_end:
            # Session present in start but absent in end: counter discontinuity
            base_c = start_chk.entries[key]
            reason = f"Session/model/task ({s_id}, {m}, {t}) was present in start but missing in end"
            discontinuity_reasons.append(reason)
            session_deltas.append(
                SessionActivityDelta(
                    session_id=s_id,
                    model=m,
                    task=t,
                    status="missing_in_current",
                    is_discontinuity=True,
                    has_baseline=True,
                    baseline_counters=base_c,
                    current_counters=None,
                    delta_counters=None,
                    reason=reason,
                )
            )

        else:
            base_c = start_chk.entries[key]
            curr_c = end_chk.entries[key]

            # Check for counter decreases / resets
            counter_decrease = (
                curr_c.api_call_count < base_c.api_call_count
                or curr_c.input_tokens < base_c.input_tokens
                or curr_c.output_tokens < base_c.output_tokens
                or curr_c.cache_read_tokens < base_c.cache_read_tokens
                or curr_c.cache_write_tokens < base_c.cache_write_tokens
            )

            if counter_decrease:
                reason = (
                    f"Counter decrease detected for ({s_id}, {m}, {t}): "
                    f"current {curr_c.to_dict()} < baseline {base_c.to_dict()}"
                )
                discontinuity_reasons.append(reason)
                session_deltas.append(
                    SessionActivityDelta(
                        session_id=s_id,
                        model=m,
                        task=t,
                        status="counter_decrease",
                        is_discontinuity=True,
                        has_baseline=True,
                        baseline_counters=base_c,
                        current_counters=curr_c,
                        delta_counters=None,  # Explicitly unknown, not negative
                        reason=reason,
                    )
                )
            else:
                # Normal monotonic progression
                delta = ActivityCounters(
                    api_call_count=curr_c.api_call_count - base_c.api_call_count,
                    input_tokens=curr_c.input_tokens - base_c.input_tokens,
                    output_tokens=curr_c.output_tokens - base_c.output_tokens,
                    cache_read_tokens=curr_c.cache_read_tokens - base_c.cache_read_tokens,
                    cache_write_tokens=curr_c.cache_write_tokens - base_c.cache_write_tokens,
                )
                clean_deltas.append(delta)
                session_deltas.append(
                    SessionActivityDelta(
                        session_id=s_id,
                        model=m,
                        task=t,
                        status="ok",
                        is_discontinuity=False,
                        has_baseline=True,
                        baseline_counters=base_c,
                        current_counters=curr_c,
                        delta_counters=delta,
                        reason=None,
                    )
                )

    has_discontinuity = any(d.is_discontinuity for d in session_deltas)

    known_total = ActivityCounters(
        api_call_count=sum(d.api_call_count for d in clean_deltas),
        input_tokens=sum(d.input_tokens for d in clean_deltas),
        output_tokens=sum(d.output_tokens for d in clean_deltas),
        cache_read_tokens=sum(d.cache_read_tokens for d in clean_deltas),
        cache_write_tokens=sum(d.cache_write_tokens for d in clean_deltas),
    )

    overall_status = "discontinuity" if has_discontinuity else "ok"
    total_delta = None if has_discontinuity else known_total

    return CheckpointDifferenceResult(
        account_id=account_id,
        start_checkpoint_id=start_chk.checkpoint_id,
        end_checkpoint_id=end_chk.checkpoint_id,
        start_observed_at=start_chk.observed_at,
        end_observed_at=end_chk.observed_at,
        status=overall_status,
        has_baseline=True,
        has_discontinuity=has_discontinuity,
        session_deltas=session_deltas,
        total_delta=total_delta,
        known_delta=known_total,
        discontinuity_reasons=discontinuity_reasons,
    )
