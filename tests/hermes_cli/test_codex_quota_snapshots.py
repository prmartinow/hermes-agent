"""Tests for Codex Quota Snapshots and Interval Derivation (Storage-Only Unit).

Validates:
1. Two Homes A-B-A Multi-Profile DB Isolation with actual imports.
2. Exact timestamps and preservation of intermediate samples (no 15-minute slot overwrites).
3. Idempotency on identical observation key / timestamp.
4. Bounded 30-day retention pruning.
5. Sanitization, allowlisting, and rejection of sensitive tokens/secrets/raw blobs.
6. Preservation of missing values, nulls, collection errors, and gap handling.
7. Interval derivation: normal depletion vs reset_or_replenishment / discontinuity
   (never asserting redeemed reset).
8. Strict account isolation: zero cross-account deltas.
9. Secondary (weekly) window isolation.
10. Architectural contract guarding against flawed session_model_usage last_seen attribution.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli.codex_quota_snapshots import (
    CodexQuotaInterval,
    CodexQuotaObservation,
    CodexQuotaSnapshotRecord,
    CodexWindowObservation,
    derive_codex_quota_intervals,
    get_codex_quota_db_path,
    init_codex_quota_snapshots_table,
    list_codex_quota_snapshots,
    prune_codex_quota_snapshots,
    record_codex_quota_snapshot,
    sanitize_codex_observation,
)
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


# ---------------------------------------------------------------------------
# 1. Two Homes A-B-A Multi-Profile Isolation Test
# ---------------------------------------------------------------------------

class TestCodexQuotaABAProfileIsolation:
    """Verifies profile-aware state.db isolation using two temporary Hermes homes (A -> B -> A)."""

    def test_aba_profile_isolation(self, tmp_path: Path):
        home_a = tmp_path / "home_a"
        home_b = tmp_path / "home_b"
        home_a.mkdir(parents=True)
        home_b.mkdir(parents=True)

        t1 = 1710000000.0
        t2 = 1710000900.0

        # Phase A1: Home A
        token_a = set_hermes_home_override(str(home_a))
        try:
            assert get_hermes_home().resolve() == home_a.resolve()
            rec_a1 = record_codex_quota_snapshot({
                "account_id": "acct-A",
                "observed_at": t1,
                "primary_used_percent": 42.0,
                "primary_reset_at": "2026-04-01T00:00:00Z",
            })
            assert rec_a1.account_id == "acct-A"

            db_a = home_a / "state.db"
            assert db_a.exists(), "state.db should be created in Home A"

            snapshots_a = list_codex_quota_snapshots()
            assert len(snapshots_a) == 1
            assert snapshots_a[0].account_id == "acct-A"
            assert snapshots_a[0].primary_used_percent == 42.0
        finally:
            reset_hermes_home_override(token_a)

        # Phase B: Home B
        token_b = set_hermes_home_override(str(home_b))
        try:
            assert get_hermes_home().resolve() == home_b.resolve()
            rec_b = record_codex_quota_snapshot({
                "account_id": "acct-B",
                "observed_at": t2,
                "primary_used_percent": 15.0,
                "primary_reset_at": "2026-04-01T05:00:00Z",
            })
            assert rec_b.account_id == "acct-B"

            db_b = home_b / "state.db"
            assert db_b.exists(), "state.db should be created in Home B"

            snapshots_b = list_codex_quota_snapshots()
            assert len(snapshots_b) == 1
            assert snapshots_b[0].account_id == "acct-B"
            assert snapshots_b[0].primary_used_percent == 15.0

            # Verify Home A was untouched by operations in Home B
            with sqlite3.connect(str(home_a / "state.db")) as conn_a:
                conn_a.row_factory = sqlite3.Row
                rows_a = conn_a.execute("SELECT * FROM codex_quota_snapshots").fetchall()
                assert len(rows_a) == 1
                assert rows_a[0]["account_id"] == "acct-A"
        finally:
            reset_hermes_home_override(token_b)

        # Phase A2: Return to Home A
        token_a2 = set_hermes_home_override(str(home_a))
        try:
            assert get_hermes_home().resolve() == home_a.resolve()
            # Verify Home A still sees only acct-A at t1
            snapshots_a2 = list_codex_quota_snapshots()
            assert len(snapshots_a2) == 1
            assert snapshots_a2[0].account_id == "acct-A"
            assert snapshots_a2[0].observed_at == t1

            # Append a new observation to Home A
            rec_a2 = record_codex_quota_snapshot({
                "account_id": "acct-A",
                "observed_at": t2,
                "primary_used_percent": 55.0,
                "primary_reset_at": "2026-04-01T00:00:00Z",
            })
            assert rec_a2.primary_used_percent == 55.0

            updated_snapshots_a = list_codex_quota_snapshots()
            assert len(updated_snapshots_a) == 2
            assert [s.observed_at for s in updated_snapshots_a] == [t1, t2]

            # Verify Home B was untouched
            with sqlite3.connect(str(home_b / "state.db")) as conn_b:
                conn_b.row_factory = sqlite3.Row
                rows_b = conn_b.execute("SELECT * FROM codex_quota_snapshots").fetchall()
                assert len(rows_b) == 1
                assert rows_b[0]["account_id"] == "acct-B"
        finally:
            reset_hermes_home_override(token_a2)


# ---------------------------------------------------------------------------
# 2. Exact Timestamps & Preservation of Intermediate Samples (No 15m Overwriting)
# ---------------------------------------------------------------------------

class TestExactTimestampsAndNoBucketing:
    """Verifies that intermediate observations within a 15m window are NOT collapsed/overwritten."""

    def test_preserves_every_sample_within_15m_window(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        base_time = 1710000000.0  # e.g. 12:00:00

        # Record 5 observations within a 10-minute span (< 15-minute slot)
        samples = [
            (base_time + 60.0, 10.0, "2026-04-01T05:00:00Z"),    # 12:01: 10%
            (base_time + 120.0, 30.0, "2026-04-01T05:00:00Z"),   # 12:02: 30%
            (base_time + 180.0, 85.0, "2026-04-01T05:00:00Z"),   # 12:03: 85%
            (base_time + 240.0, 5.0, "2026-04-01T10:00:00Z"),    # 12:04: reset occurred! drops to 5%
            (base_time + 300.0, 12.0, "2026-04-01T10:00:00Z"),   # 12:05: 12%
        ]

        for obs_time, used, reset_at in samples:
            record_codex_quota_snapshot({
                "account_id": "acct-live",
                "observed_at": obs_time,
                "primary_used_percent": used,
                "primary_reset_at": reset_at,
            }, db_path=db_path)

        stored = list_codex_quota_snapshots(db_path=db_path)
        assert len(stored) == 5, "All 5 samples must be preserved; none should be bucket-overwritten"

        observed_times = [s.observed_at for s in stored]
        expected_times = [s[0] for s in samples]
        assert observed_times == expected_times

        used_values = [s.primary_used_percent for s in stored]
        expected_used = [s[1] for s in samples]
        assert used_values == expected_used

        # Derive intervals across these 5 samples: the reset evidence at 12:04 must be present!
        intervals = derive_codex_quota_intervals(stored, window_id="primary")
        assert len(intervals) == 4
        # Interval 0: 10% -> 30% (+20% depletion)
        assert intervals[0].kind == "depletion"
        assert intervals[0].delta_used_percent == 20.0
        # Interval 1: 30% -> 85% (+55% depletion)
        assert intervals[1].kind == "depletion"
        assert intervals[1].delta_used_percent == 55.0
        # Interval 2: 85% -> 5% (reset_or_replenishment)
        assert intervals[2].kind == "reset_or_replenishment"
        assert intervals[2].reset_at_changed is True
        assert intervals[2].delta_used_percent == -80.0
        # Interval 3: 5% -> 12% (+7% depletion)
        assert intervals[3].kind == "depletion"
        assert intervals[3].delta_used_percent == 7.0


# ---------------------------------------------------------------------------
# 3. Idempotency on Identical Observation Key
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Verifies that re-ingesting an identical observation is strictly idempotent."""

    def test_idempotent_duplicate_timestamp(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        t = 1710000000.0

        obs1 = {
            "account_id": "acct-1",
            "observed_at": t,
            "primary_used_percent": 25.0,
            "primary_reset_at": "2026-04-01T00:00:00Z",
        }
        rec1 = record_codex_quota_snapshot(obs1, db_path=db_path)
        assert rec1.primary_used_percent == 25.0

        # Re-record with same (account_id, observed_at) but updated percent
        obs2 = {
            "account_id": "acct-1",
            "observed_at": t,
            "primary_used_percent": 28.0,
            "primary_reset_at": "2026-04-01T00:00:00Z",
        }
        rec2 = record_codex_quota_snapshot(obs2, db_path=db_path)
        assert rec2.id == rec1.id
        assert rec2.primary_used_percent == 28.0

        all_records = list_codex_quota_snapshots(db_path=db_path)
        assert len(all_records) == 1, "Duplicate key must update in place without row inflation"

    def test_idempotent_explicit_observation_id(self, tmp_path: Path):
        db_path = tmp_path / "state.db"

        obs1 = {
            "account_id": "acct-1",
            "observation_id": "obs_pinned_12345",
            "observed_at": 1710000100.0,
            "primary_used_percent": 50.0,
        }
        rec1 = record_codex_quota_snapshot(obs1, db_path=db_path)

        # Re-record with identical observation_id
        obs2 = {
            "account_id": "acct-1",
            "observation_id": "obs_pinned_12345",
            "observed_at": 1710000100.0,
            "primary_used_percent": 52.0,
        }
        rec2 = record_codex_quota_snapshot(obs2, db_path=db_path)
        assert rec2.id == rec1.id
        assert rec2.primary_used_percent == 52.0

        all_records = list_codex_quota_snapshots(db_path=db_path)
        assert len(all_records) == 1


# ---------------------------------------------------------------------------
# 4. Bounded Retention (30 Days)
# ---------------------------------------------------------------------------

class TestBoundedRetention:
    """Verifies rolling 30-day retention pruning."""

    def test_automatic_retention_pruning_on_insert(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        t_now = 1710000000.0
        sec_35_days = 35 * 86400.0
        sec_10_days = 10 * 86400.0

        # Old snapshot (35 days before t_now)
        record_codex_quota_snapshot({
            "account_id": "acct-retention",
            "observed_at": t_now - sec_35_days,
            "primary_used_percent": 10.0,
        }, db_path=db_path, retention_days=30)

        # Recent snapshot (10 days before t_now)
        record_codex_quota_snapshot({
            "account_id": "acct-retention",
            "observed_at": t_now - sec_10_days,
            "primary_used_percent": 20.0,
        }, db_path=db_path, retention_days=30)

        # Verify both exist prior to current insert
        before = list_codex_quota_snapshots(db_path=db_path)
        assert len(before) == 2

        # Insert snapshot at t_now with 30-day retention
        record_codex_quota_snapshot({
            "account_id": "acct-retention",
            "observed_at": t_now,
            "primary_used_percent": 30.0,
        }, db_path=db_path, retention_days=30)

        after = list_codex_quota_snapshots(db_path=db_path)
        assert len(after) == 2, "35-day-old snapshot must be pruned; 10-day and current must remain"
        retained_times = [s.observed_at for s in after]
        assert (t_now - sec_35_days) not in retained_times
        assert (t_now - sec_10_days) in retained_times
        assert t_now in retained_times

    def test_explicit_prune_helper(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        t_base = 1000.0

        for offset in [0, 50, 100, 200]:
            record_codex_quota_snapshot({
                "account_id": "acct-p",
                "observed_at": t_base + offset,
                "primary_used_percent": float(offset) / 4.0,
            }, db_path=db_path, retention_days=0)  # disable auto-prune

        assert len(list_codex_quota_snapshots(db_path=db_path)) == 4

        # Prune everything before t_base + 90
        pruned_count = prune_codex_quota_snapshots(before_timestamp=t_base + 90, db_path=db_path)
        assert pruned_count == 2

        remaining = list_codex_quota_snapshots(db_path=db_path)
        assert len(remaining) == 2
        assert [r.observed_at for r in remaining] == [t_base + 100, t_base + 200]


# ---------------------------------------------------------------------------
# 5. Sanitization, Allowlisting & Security (No Tokens / Raw Blobs)
# ---------------------------------------------------------------------------

class TestSanitizationAndSecurity:
    """Verifies strict allowlisting and rejection of sensitive tokens/secrets/raw blobs."""

    def test_sensitive_fields_discarded(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        dirty_input = {
            "account_id": "acct-secure",
            "observed_at": 1710000000.0,
            "primary_used_percent": 50.0,
            # Sensitive fields that must NEVER enter SQLite:
            "token": "sk-proj-supersecrettoken12345",
            "access_token": "bearer-secret-xyz",
            "authorization": "Bearer eyJhbGciOi...",
            "credits_balance": 150.0,
            "has_credits": True,
            "billing": {"card": "4111222233334444"},
            "raw_payload": {"sensitive_internal_meta": "leak"},
            "periods_json": "[{'secret': 'data'}]",
            "session_model_usage": {"fake_split": 0.5},
        }

        rec = record_codex_quota_snapshot(dirty_input, db_path=db_path)
        assert rec.account_id == "acct-secure"
        assert not hasattr(rec, "token")
        assert not hasattr(rec, "credits_balance")

        # Direct SQLite inspection to guarantee no forbidden columns exist or were filled
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM codex_quota_snapshots WHERE id = ?", (rec.id,)).fetchone()
            col_names = set(row.keys())
            forbidden = {"token", "access_token", "authorization", "credits_balance",
                         "has_credits", "billing", "raw_payload", "periods_json", "session_model_usage"}
            assert col_names.isdisjoint(forbidden), f"Found forbidden columns in SQLite: {col_names & forbidden}"

    def test_type_validation_rejects_booleans_as_numeric(self):
        # Python bool is an int subclass; booleans must be explicitly rejected
        with pytest.raises(ValueError, match="numeric"):
            sanitize_codex_observation({
                "account_id": "acct-1",
                "observed_at": 100.0,
                "primary_used_percent": True,
            })

        with pytest.raises(ValueError, match="numeric"):
            sanitize_codex_observation({
                "account_id": "acct-1",
                "observed_at": False,
            })

    def test_rejects_negative_or_infinite_values(self):
        with pytest.raises(ValueError):
            sanitize_codex_observation({
                "account_id": "acct-1",
                "observed_at": -5.0,
            })

        with pytest.raises(ValueError):
            sanitize_codex_observation({
                "account_id": "acct-1",
                "observed_at": float("inf"),
            })

        with pytest.raises(ValueError):
            sanitize_codex_observation({
                "account_id": "acct-1",
                "observed_at": 100.0,
                "primary_used_percent": 150.0,  # exceeds 100%
            })

    def test_rejects_empty_account_id(self):
        with pytest.raises(ValueError, match="account_id"):
            sanitize_codex_observation({
                "account_id": "   ",
                "observed_at": 100.0,
            })


# ---------------------------------------------------------------------------
# 6. Preservation of Missing Values, Nulls, Collection Errors & Gaps
# ---------------------------------------------------------------------------

class TestMissingValuesAndGaps:
    """Verifies that collection errors and missing values are preserved and flagged as gaps."""

    def test_preserves_error_observation_and_flags_gap_interval(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        t0 = 1710000000.0
        t1 = 1710000300.0
        t2 = 1710000600.0

        # Snapshot 0: normal
        record_codex_quota_snapshot({
            "account_id": "acct-gap",
            "observed_at": t0,
            "primary_used_percent": 20.0,
            "primary_reset_at": "2026-04-01T00:00:00Z",
            "status": "ok",
        }, db_path=db_path)

        # Snapshot 1: provider rate-limit error (missing usage)
        record_codex_quota_snapshot({
            "account_id": "acct-gap",
            "observed_at": t1,
            "primary_used_percent": None,
            "primary_reset_at": None,
            "status": "error",
            "error_code": "RATE_LIMIT_429",
            "error_message": "Rate limit exceeded against /backend-api/codex/usage",
        }, db_path=db_path)

        # Snapshot 2: recovered
        record_codex_quota_snapshot({
            "account_id": "acct-gap",
            "observed_at": t2,
            "primary_used_percent": 25.0,
            "primary_reset_at": "2026-04-01T00:00:00Z",
            "status": "ok",
        }, db_path=db_path)

        stored = list_codex_quota_snapshots(db_path=db_path)
        assert len(stored) == 3
        assert stored[1].status == "error"
        assert stored[1].error_code == "RATE_LIMIT_429"
        assert stored[1].primary_used_percent is None

        intervals = derive_codex_quota_intervals(stored, window_id="primary")
        assert len(intervals) == 2

        # Interval [t0, t1]: spans into an error snapshot -> gap
        assert intervals[0].kind == "gap"
        assert intervals[0].status == "gap"
        assert intervals[0].delta_used_percent is None

        # Interval [t1, t2]: spans out of an error snapshot -> gap
        assert intervals[1].kind == "gap"
        assert intervals[1].status == "gap"
        assert intervals[1].delta_used_percent is None


# ---------------------------------------------------------------------------
# 7. Interval Derivation: Normal Depletion vs Reset / Discontinuity
# ---------------------------------------------------------------------------

class TestIntervalDerivationSemantics:
    """Verifies classification of depletion, reset_or_replenishment, and discontinuity."""

    def test_normal_depletion(self):
        records = [
            {"account_id": "a", "observed_at": 100.0, "primary_used_percent": 10.0, "primary_reset_at": "R1"},
            {"account_id": "a", "observed_at": 200.0, "primary_used_percent": 30.0, "primary_reset_at": "R1"},
            {"account_id": "a", "observed_at": 300.0, "primary_used_percent": 30.0, "primary_reset_at": "R1"},
        ]
        intervals = derive_codex_quota_intervals(records, window_id="primary")
        assert len(intervals) == 2

        # 10% -> 30%: depletion
        assert intervals[0].kind == "depletion"
        assert intervals[0].status == "ok"
        assert intervals[0].delta_used_percent == 20.0
        assert intervals[0].reset_at_changed is False

        # 30% -> 30%: unchanged
        assert intervals[1].kind == "unchanged"
        assert intervals[1].status == "ok"
        assert intervals[1].delta_used_percent == 0.0
        assert intervals[1].reset_at_changed is False

    def test_reset_or_replenishment_when_reset_at_changes(self):
        """Negative delta with reset_at shift must classify as reset_or_replenishment, not assert redeemed credit."""
        records = [
            {"account_id": "a", "observed_at": 100.0, "primary_used_percent": 95.0, "primary_reset_at": "R1"},
            {"account_id": "a", "observed_at": 200.0, "primary_used_percent": 5.0, "primary_reset_at": "R2"},
        ]
        intervals = derive_codex_quota_intervals(records, window_id="primary")
        assert len(intervals) == 1

        assert intervals[0].kind == "reset_or_replenishment"
        assert intervals[0].status == "reset_or_replenishment"
        assert intervals[0].reset_at_changed is True
        assert intervals[0].delta_used_percent == -90.0

    def test_negative_delta_without_reset_at_change_is_discontinuity(self):
        """Negative delta without reset_at change indicates backend adjustment or replenishment."""
        records = [
            {"account_id": "a", "observed_at": 100.0, "primary_used_percent": 60.0, "primary_reset_at": "R1"},
            {"account_id": "a", "observed_at": 200.0, "primary_used_percent": 40.0, "primary_reset_at": "R1"},
        ]
        intervals = derive_codex_quota_intervals(records, window_id="primary")
        assert len(intervals) == 1

        assert intervals[0].kind == "reset_or_replenishment"
        assert intervals[0].status == "discontinuity"
        assert intervals[0].reset_at_changed is False
        assert intervals[0].delta_used_percent == -20.0


# ---------------------------------------------------------------------------
# 8. Strict Account Isolation: Zero Cross-Account Deltas
# ---------------------------------------------------------------------------

class TestAccountIsolation:
    """Verifies that intervals are never derived across differing accounts."""

    def test_no_cross_account_deltas(self):
        records = [
            # Interleaved observations from two accounts
            {"account_id": "acct-1", "observed_at": 100.0, "primary_used_percent": 10.0, "primary_reset_at": "R1"},
            {"account_id": "acct-2", "observed_at": 150.0, "primary_used_percent": 80.0, "primary_reset_at": "R_A"},
            {"account_id": "acct-1", "observed_at": 200.0, "primary_used_percent": 25.0, "primary_reset_at": "R1"},
            {"account_id": "acct-2", "observed_at": 250.0, "primary_used_percent": 90.0, "primary_reset_at": "R_A"},
        ]

        # Derive intervals across the combined record stream
        intervals = derive_codex_quota_intervals(records, window_id="primary")
        assert len(intervals) == 2, "Should derive exactly 1 interval for acct-1 and 1 for acct-2"

        # Verify acct-1 interval
        inv_acct1 = [i for i in intervals if i.account_id == "acct-1"]
        assert len(inv_acct1) == 1
        assert inv_acct1[0].start_time == 100.0
        assert inv_acct1[0].end_time == 200.0
        assert inv_acct1[0].delta_used_percent == 15.0

        # Verify acct-2 interval
        inv_acct2 = [i for i in intervals if i.account_id == "acct-2"]
        assert len(inv_acct2) == 1
        assert inv_acct2[0].start_time == 150.0
        assert inv_acct2[0].end_time == 250.0
        assert inv_acct2[0].delta_used_percent == 10.0

        # Verify ZERO cross-account intervals exist
        for inv in intervals:
            assert inv.account_id in ("acct-1", "acct-2")


# ---------------------------------------------------------------------------
# 9. Secondary (Weekly) Window Isolation
# ---------------------------------------------------------------------------

class TestSecondaryWindowIsolation:
    """Verifies that primary and secondary quota windows derive independently."""

    def test_secondary_window_derivation(self):
        records = [
            {
                "account_id": "acct-w",
                "observed_at": 1000.0,
                "primary_used_percent": 90.0,
                "primary_reset_at": "P1",
                "secondary_used_percent": 15.0,
                "secondary_reset_at": "S1",
            },
            {
                "account_id": "acct-w",
                "observed_at": 2000.0,
                "primary_used_percent": 10.0,  # primary reset
                "primary_reset_at": "P2",
                "secondary_used_percent": 18.0,  # secondary continued depletion
                "secondary_reset_at": "S1",
            },
        ]

        # Primary window saw reset
        p_intervals = derive_codex_quota_intervals(records, window_id="primary")
        assert len(p_intervals) == 1
        assert p_intervals[0].kind == "reset_or_replenishment"
        assert p_intervals[0].reset_at_changed is True

        # Secondary window saw normal depletion (+3.0%)
        s_intervals = derive_codex_quota_intervals(records, window_id="secondary")
        assert len(s_intervals) == 1
        assert s_intervals[0].kind == "depletion"
        assert s_intervals[0].reset_at_changed is False
        assert s_intervals[0].delta_used_percent == 3.0


# ---------------------------------------------------------------------------
# 10. Architectural Contract: Correction of Flawed Attribution
# ---------------------------------------------------------------------------

class TestAttributionArchitecturalContract:
    """Guards against invalid token-summing attribution patterns."""

    def test_attribution_contract_invariants(self):
        """Asserts that cumulative counter summing at last_seen is formally prohibited.

        Why summing cumulative session_model_usage at last_seen across [T0, T1] fails:
        1. session_model_usage stores lifetime cumulative token sums. Summing rows
           where T0 <= last_seen <= T1 falsely assigns an entire session's multi-day
           token total to this small time window.
        2. Sessions active in [T0, T1] that run further calls at T2 > T1 have their
           last_seen updated past T1, omitting them from the window entirely.
        3. Subscription quota limits are non-linear, tier-dependent, and shared with
           external IDEs, CLI, and ChatGPT Web.
        """
        # Verified invariant: Storage unit provides interval deltas without synthesizing local token splits
        intervals = derive_codex_quota_intervals([
            {"account_id": "a", "observed_at": 100.0, "primary_used_percent": 20.0},
            {"account_id": "a", "observed_at": 200.0, "primary_used_percent": 25.0},
        ])
        assert len(intervals) == 1
        inv = intervals[0]
        # Invariant: interval exposes observed account facts only
        assert inv.delta_used_percent == 5.0
        assert not hasattr(inv, "attributed_tokens")
        assert not hasattr(inv, "token_quota_split")


# ---------------------------------------------------------------------------
# 11. Read-Only Guarantees & SQLite Authorizer Regression Tests
# ---------------------------------------------------------------------------

class TestCodexQuotaReadOnlyGuarantees:
    """Verifies genuine read-only SQLite guarantees on read paths."""

    def test_ro_connection_authorizer_denies_all_mutations(self, tmp_path: Path):
        """SQLite authorizer on _connect_ro_db explicitly denies INSERT/UPDATE/DELETE/CREATE/ALTER/DROP."""
        from hermes_cli.codex_quota_snapshots import _connect_ro_db

        db_path = tmp_path / "test_auth.db"
        record_codex_quota_snapshot({
            "account_id": "acct-test",
            "observed_at": 1000.0,
            "primary_used_percent": 10.0,
        }, db_path=db_path)

        ro_conn = _connect_ro_db(db_path)
        assert ro_conn is not None

        try:
            # Reads must succeed
            rows = ro_conn.execute("SELECT * FROM codex_quota_snapshots").fetchall()
            assert len(rows) == 1

            # INSERT must be denied
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute(
                    "INSERT INTO codex_quota_snapshots (observation_id, observed_at, time_label, account_id, created_at) "
                    "VALUES ('obs_2', 2000.0, 'time', 'acct-test', 2000.0)"
                )

            # UPDATE must be denied
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute("UPDATE codex_quota_snapshots SET primary_used_percent = 99.0")

            # DELETE must be denied
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute("DELETE FROM codex_quota_snapshots")

            # CREATE TABLE must be denied
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute("CREATE TABLE evil_table (id INTEGER)")

            # CREATE INDEX must be denied
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute("CREATE INDEX evil_idx ON codex_quota_snapshots (plan_type)")

            # ALTER TABLE must be denied
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute("ALTER TABLE codex_quota_snapshots ADD COLUMN evil_col TEXT")

            # DROP TABLE must be denied
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute("DROP TABLE codex_quota_snapshots")

            # ATTACH must be denied
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute("ATTACH DATABASE ':memory:' AS attached_db")
        finally:
            ro_conn.close()

    def test_chmod_alone_inadequate_full_write_perms_still_enforces_readonly(self, tmp_path: Path):
        """Even with 0o666 (world read-write) disk permissions, read connection enforces read-only."""
        import os
        from hermes_cli.codex_quota_snapshots import _connect_ro_db

        db_path = tmp_path / "test_perms.db"
        record_codex_quota_snapshot({
            "account_id": "acct-test",
            "observed_at": 1000.0,
            "primary_used_percent": 10.0,
        }, db_path=db_path)

        os.chmod(db_path, 0o666)
        ro_conn = _connect_ro_db(db_path)
        assert ro_conn is not None
        try:
            with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
                ro_conn.execute("DELETE FROM codex_quota_snapshots")
        finally:
            ro_conn.close()

    def test_read_helpers_never_execute_ddl_on_empty_db(self, tmp_path: Path):
        """Calling list_codex_quota_snapshots and derive_codex_quota_intervals on an empty DB executes zero DDL."""
        db_path = tmp_path / "empty.db"
        with sqlite3.connect(str(db_path)) as c:
            pass

        with sqlite3.connect(str(db_path)) as c:
            tables_before = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(tables_before) == 0

        snapshots = list_codex_quota_snapshots(db_path=db_path)
        assert snapshots == []

        intervals = derive_codex_quota_intervals(db_path=db_path)
        assert intervals == []

        with sqlite3.connect(str(db_path)) as c:
            tables_after = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(tables_after) == 0

    def test_schema_and_data_before_after_identical(self, tmp_path: Path):
        """Executing read helpers preserves identical schema and records before and after."""
        db_path = tmp_path / "populated.db"
        record_codex_quota_snapshot({
            "account_id": "acct-alpha",
            "observed_at": 1000.0,
            "primary_used_percent": 25.0,
            "primary_reset_at": "2026-04-01T00:00:00Z",
        }, db_path=db_path)
        record_codex_quota_snapshot({
            "account_id": "acct-alpha",
            "observed_at": 2000.0,
            "primary_used_percent": 30.0,
            "primary_reset_at": "2026-04-01T00:00:00Z",
        }, db_path=db_path)

        def _get_db_state(path):
            with sqlite3.connect(str(path)) as c:
                c.row_factory = sqlite3.Row
                schema = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
                data = c.execute("SELECT * FROM codex_quota_snapshots ORDER BY id").fetchall()
                return [dict(r) for r in schema], [dict(r) for r in data]

        schema_before, data_before = _get_db_state(db_path)

        res1 = list_codex_quota_snapshots(db_path=db_path)
        assert len(res1) == 2
        res2 = list_codex_quota_snapshots(account_id="acct-alpha", limit=1, db_path=db_path)
        assert len(res2) == 1
        invs = derive_codex_quota_intervals(account_id="acct-alpha", db_path=db_path)
        assert len(invs) == 1

        schema_after, data_after = _get_db_state(db_path)
        assert schema_before == schema_after
        assert data_before == data_after

    def test_missing_db_returns_empty_and_does_not_create_file(self, tmp_path: Path):
        """Calling read helpers on missing DB path does not create the file or parent directory."""
        nonexistent = tmp_path / "nonexistent_dir" / "missing.db"
        assert not nonexistent.exists()

        snapshots = list_codex_quota_snapshots(db_path=nonexistent)
        assert snapshots == []
        assert not nonexistent.exists()

        invs = derive_codex_quota_intervals(db_path=nonexistent)
        assert invs == []
        assert not nonexistent.exists()

    def test_prune_does_not_init_table_if_missing(self, tmp_path: Path):
        """prune_codex_quota_snapshots does not execute CREATE TABLE on empty DB (write init only in record)."""
        db_path = tmp_path / "empty_prune.db"
        with sqlite3.connect(str(db_path)) as c:
            pass

        pruned = prune_codex_quota_snapshots(db_path=db_path)
        assert pruned == 0

        with sqlite3.connect(str(db_path)) as c:
            tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(tables) == 0

    def test_error_message_raw_strings_never_in_interval_description(self):
        """Interval description uses stable error codes and never leaks raw error_message strings."""
        leak_secret = "INTERNAL_LEAK_BEARER_TOKEN_9999"
        obs1 = {
            "account_id": "acct-err",
            "observed_at": 100.0,
            "status": "error",
            "error_code": "RATE_LIMIT",
            "error_message": f"Fatal upstream failure: {leak_secret}",
        }
        obs2 = {
            "account_id": "acct-err",
            "observed_at": 200.0,
            "status": "ok",
            "primary_used_percent": 10.0,
        }

        intervals = derive_codex_quota_intervals([obs1, obs2])
        assert len(intervals) == 1
        inv = intervals[0]
        assert inv.kind == "gap"
        assert inv.status == "gap"
        assert "RATE_LIMIT" in inv.description
        assert leak_secret not in inv.description
        assert "Fatal upstream failure" not in inv.description
