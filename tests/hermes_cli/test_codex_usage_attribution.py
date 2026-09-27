"""Tests for small activity checkpoint storage and Codex usage attribution."""

import sqlite3
import pytest

from hermes_cli.codex_usage_attribution import (
    ActivityCounters,
    CodexActivityCheckpoint,
    capture_checkpoint,
    derive_checkpoint_differences,
    get_checkpoint,
    get_latest_checkpoint,
    init_codex_activity_checkpoints_table,
    list_checkpoints,
    prune_activity_checkpoints,
)

SESSION_MODEL_USAGE_DDL = """
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
"""


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary SQLite database with session_model_usage schema."""
    db_file = tmp_path / "test_state.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(SESSION_MODEL_USAGE_DDL)
    conn.commit()
    conn.close()
    return db_file


def _insert_or_update_usage(
    db_path,
    session_id,
    model,
    billing_provider="openai-codex",
    task="",
    api_call_count=0,
    input_tokens=0,
    output_tokens=0,
    cache_read_tokens=0,
    cache_write_tokens=0,
    observed_time=1000.0,
):
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, task,
            api_call_count, input_tokens, output_tokens,
            cache_read_tokens, cache_write_tokens,
            first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, model, billing_provider, billing_base_url, billing_mode, task)
        DO UPDATE SET
            api_call_count = excluded.api_call_count,
            input_tokens = excluded.input_tokens,
            output_tokens = excluded.output_tokens,
            cache_read_tokens = excluded.cache_read_tokens,
            cache_write_tokens = excluded.cache_write_tokens,
            last_seen = excluded.last_seen
    """, (
        session_id, model, billing_provider, task,
        api_call_count, input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens,
        observed_time, observed_time,
    ))
    conn.commit()
    conn.close()


def test_two_sessions_delta(temp_db):
    """Test 2 concurrent sessions: counters increment and checkpoint delta attributes exact usage."""
    # Step 1: Initial activity for session-1 and session-2
    _insert_or_update_usage(
        temp_db, "sess-1", "gpt-5.4", api_call_count=5,
        input_tokens=1000, output_tokens=200, cache_read_tokens=100, cache_write_tokens=50,
        observed_time=1000.0,
    )
    _insert_or_update_usage(
        temp_db, "sess-2", "gpt-5.4", api_call_count=2,
        input_tokens=500, output_tokens=100, cache_read_tokens=50, cache_write_tokens=25,
        observed_time=1000.0,
    )

    # Capture baseline checkpoint
    chk_start = capture_checkpoint("acct-primary", observed_at=1000.0, db_path=temp_db)
    assert chk_start.account_id == "acct-primary"
    assert len(chk_start.entries) == 2
    c1 = chk_start.get_counters("sess-1", "gpt-5.4", "")
    assert c1 is not None
    assert c1.api_call_count == 5
    assert c1.input_tokens == 1000
    assert c1.cache_tokens == 150

    # Step 2: More activity occurs in both sessions
    _insert_or_update_usage(
        temp_db, "sess-1", "gpt-5.4", api_call_count=8,
        input_tokens=1800, output_tokens=350, cache_read_tokens=150, cache_write_tokens=70,
        observed_time=1100.0,
    )
    _insert_or_update_usage(
        temp_db, "sess-2", "gpt-5.4", api_call_count=5,
        input_tokens=1100, output_tokens=250, cache_read_tokens=80, cache_write_tokens=40,
        observed_time=1100.0,
    )

    # Capture end checkpoint
    chk_end = capture_checkpoint("acct-primary", observed_at=1100.0, db_path=temp_db)
    assert len(chk_end.entries) == 2

    # Step 3: Derive differences
    diff = derive_checkpoint_differences(chk_start, chk_end, db_path=temp_db)
    assert diff.status == "ok"
    assert diff.has_baseline is True
    assert diff.has_discontinuity is False
    assert len(diff.session_deltas) == 2

    # Check session-1 delta: (8-5=3 calls, 1800-1000=800 input, 350-200=150 output)
    d1 = next(d for d in diff.session_deltas if d.session_id == "sess-1")
    assert d1.status == "ok"
    assert d1.is_discontinuity is False
    assert d1.has_baseline is True
    assert d1.delta_counters is not None
    assert d1.delta_counters.api_call_count == 3
    assert d1.delta_counters.input_tokens == 800
    assert d1.delta_counters.output_tokens == 150
    assert d1.delta_counters.cache_read_tokens == 50
    assert d1.delta_counters.cache_write_tokens == 20
    assert d1.delta_counters.cache_tokens == 70

    # Check session-2 delta: (5-2=3 calls, 1100-500=600 input, 250-100=150 output)
    d2 = next(d for d in diff.session_deltas if d.session_id == "sess-2")
    assert d2.status == "ok"
    assert d2.delta_counters is not None
    assert d2.delta_counters.api_call_count == 3
    assert d2.delta_counters.input_tokens == 600
    assert d2.delta_counters.output_tokens == 150

    # Check total delta
    assert diff.total_delta is not None
    assert diff.total_delta.api_call_count == 6
    assert diff.total_delta.input_tokens == 1400
    assert diff.total_delta.output_tokens == 300
    assert diff.total_delta.cache_tokens == 115


def test_model_switch_attribution(temp_db):
    """Test model switch within a session: counters are partitioned by model and task."""
    # Session starts on gpt-5.4
    _insert_or_update_usage(
        temp_db, "sess-switch", "gpt-5.4", api_call_count=10,
        input_tokens=2000, output_tokens=400, observed_time=1000.0,
    )
    chk_1 = capture_checkpoint("acct-primary", observed_at=1000.0, db_path=temp_db)

    # Session switches to gpt-5.4-mini and makes calls on both
    _insert_or_update_usage(
        temp_db, "sess-switch", "gpt-5.4", api_call_count=10,
        input_tokens=2000, output_tokens=400, observed_time=1050.0,
    )
    _insert_or_update_usage(
        temp_db, "sess-switch", "gpt-5.4-mini", api_call_count=15,
        input_tokens=3000, output_tokens=600, observed_time=1050.0,
    )
    chk_2 = capture_checkpoint("acct-primary", observed_at=1050.0, db_path=temp_db)

    # More calls made ONLY on gpt-5.4-mini
    _insert_or_update_usage(
        temp_db, "sess-switch", "gpt-5.4-mini", api_call_count=20,
        input_tokens=4000, output_tokens=800, observed_time=1100.0,
    )
    chk_3 = capture_checkpoint("acct-primary", observed_at=1100.0, db_path=temp_db)

    # Diff between chk_2 and chk_3:
    # gpt-5.4 should have delta = 0
    # gpt-5.4-mini should have delta = 5 calls, 1000 in, 200 out
    diff = derive_checkpoint_differences(chk_2, chk_3, db_path=temp_db)
    assert diff.status == "ok"
    assert diff.has_discontinuity is False

    d_main = next(d for d in diff.session_deltas if d.model == "gpt-5.4")
    assert d_main.status == "ok"
    assert d_main.delta_counters.api_call_count == 0
    assert d_main.delta_counters.input_tokens == 0

    d_mini = next(d for d in diff.session_deltas if d.model == "gpt-5.4-mini")
    assert d_mini.status == "ok"
    assert d_mini.delta_counters.api_call_count == 5
    assert d_mini.delta_counters.input_tokens == 1000
    assert d_mini.delta_counters.output_tokens == 200


def test_missing_baseline(temp_db):
    """Test missing baselines: start is None or new session missing baseline.

    Must NOT sum lifetime usage by last_seen; explicitly marks status as unknown/missing_baseline.
    """
    _insert_or_update_usage(
        temp_db, "sess-old", "gpt-5.4", api_call_count=50,
        input_tokens=100000, output_tokens=20000, observed_time=1000.0,
    )
    chk_end = capture_checkpoint("acct-primary", observed_at=1000.0, db_path=temp_db)

    # Case A: Entire baseline checkpoint is missing (start is None)
    diff_no_base = derive_checkpoint_differences(None, chk_end, db_path=temp_db)
    assert diff_no_base.has_baseline is False
    assert diff_no_base.has_discontinuity is True
    assert diff_no_base.status == "missing_baseline"
    assert diff_no_base.total_delta is None  # CRITICAL: not lifetime sum!
    assert len(diff_no_base.session_deltas) == 1
    d_no_base = diff_no_base.session_deltas[0]
    assert d_no_base.status == "missing_baseline"
    assert d_no_base.is_discontinuity is True
    assert d_no_base.has_baseline is False
    assert d_no_base.delta_counters is None  # Delta cannot be assumed

    # Case B: Baseline exists, but a new session is present in end that had no baseline
    chk_start = chk_end  # chk_start has only "sess-old"
    _insert_or_update_usage(
        temp_db, "sess-old", "gpt-5.4", api_call_count=52,
        input_tokens=102000, output_tokens=20400, observed_time=1100.0,
    )
    # Brand new session appears without a baseline in chk_start
    _insert_or_update_usage(
        temp_db, "sess-new", "gpt-5.4", api_call_count=10,
        input_tokens=5000, output_tokens=1000, observed_time=1100.0,
    )
    chk_next = capture_checkpoint("acct-primary", observed_at=1100.0, db_path=temp_db)

    diff_mixed = derive_checkpoint_differences(chk_start, chk_next, db_path=temp_db)
    assert diff_mixed.has_baseline is True
    assert diff_mixed.has_discontinuity is True
    assert diff_mixed.status == "discontinuity"
    assert diff_mixed.total_delta is None

    # sess-old is ok
    d_old = next(d for d in diff_mixed.session_deltas if d.session_id == "sess-old")
    assert d_old.status == "ok"
    assert d_old.is_discontinuity is False
    assert d_old.delta_counters.api_call_count == 2
    assert d_old.delta_counters.input_tokens == 2000

    # sess-new is missing_baseline
    d_new = next(d for d in diff_mixed.session_deltas if d.session_id == "sess-new")
    assert d_new.status == "missing_baseline"
    assert d_new.is_discontinuity is True
    assert d_new.has_baseline is False
    assert d_new.delta_counters is None


def test_counter_decrease_and_reset(temp_db):
    """Test counter decrease / reset: flagged as explicit discontinuity, never negative delta."""
    _insert_or_update_usage(
        temp_db, "sess-reset", "gpt-5.4", api_call_count=20,
        input_tokens=5000, output_tokens=1000, observed_time=1000.0,
    )
    chk_1 = capture_checkpoint("acct-primary", observed_at=1000.0, db_path=temp_db)

    # Counter decrease / reset occurs (e.g. database repair, session counter dropped)
    _insert_or_update_usage(
        temp_db, "sess-reset", "gpt-5.4", api_call_count=5,
        input_tokens=1200, output_tokens=250, observed_time=1100.0,
    )
    chk_2 = capture_checkpoint("acct-primary", observed_at=1100.0, db_path=temp_db)

    diff = derive_checkpoint_differences(chk_1, chk_2, db_path=temp_db)
    assert diff.has_discontinuity is True
    assert diff.status == "discontinuity"
    assert diff.total_delta is None

    d = diff.session_deltas[0]
    assert d.session_id == "sess-reset"
    assert d.status == "counter_decrease"
    assert d.is_discontinuity is True
    assert d.delta_counters is None  # MUST NOT be -15 calls, -3800 tokens


def test_account_isolation(temp_db):
    """Test strict account isolation: cannot derive diff across different accounts."""
    _insert_or_update_usage(
        temp_db, "sess-acc1", "gpt-5.4", api_call_count=5,
        input_tokens=1000, output_tokens=200, observed_time=1000.0,
    )
    chk_acct1 = capture_checkpoint("acct-alice", observed_at=1000.0, db_path=temp_db)

    _insert_or_update_usage(
        temp_db, "sess-acc2", "gpt-5.4", api_call_count=10,
        input_tokens=2000, output_tokens=400, observed_time=1000.0,
    )
    chk_acct2 = capture_checkpoint("acct-bob", observed_at=1000.0, db_path=temp_db)

    # Account isolation in queries
    latest_alice = get_latest_checkpoint("acct-alice", db_path=temp_db)
    assert latest_alice is not None
    assert latest_alice.account_id == "acct-alice"
    assert latest_alice.checkpoint_id == chk_acct1.checkpoint_id

    latest_bob = get_latest_checkpoint("acct-bob", db_path=temp_db)
    assert latest_bob is not None
    assert latest_bob.account_id == "acct-bob"
    assert latest_bob.checkpoint_id == chk_acct2.checkpoint_id

    # Cross-account diff MUST raise ValueError
    with pytest.raises(ValueError, match="Account mismatch"):
        derive_checkpoint_differences(chk_acct1, chk_acct2, db_path=temp_db)


def test_retention_pruning_30_days(temp_db):
    """Test retention pruning: records older than 30 days are pruned."""
    t_old = 1000.0
    t_fresh = t_old + (31 * 86400.0)

    # Capture old checkpoint
    chk_old = capture_checkpoint("acct-retention", observed_at=t_old, db_path=temp_db, retention_days=30)
    assert get_checkpoint(chk_old.checkpoint_id, db_path=temp_db) is not None

    # Capture new checkpoint 31 days later -> should prune chk_old
    chk_fresh = capture_checkpoint("acct-retention", observed_at=t_fresh, db_path=temp_db, retention_days=30)
    assert get_checkpoint(chk_fresh.checkpoint_id, db_path=temp_db) is not None

    # chk_old should be pruned
    assert get_checkpoint(chk_old.checkpoint_id, db_path=temp_db) is None


def test_filter_only_openai_codex_provider(temp_db):
    """Test that only openai-codex billing_provider rows are copied."""
    _insert_or_update_usage(
        temp_db, "sess-codex", "gpt-5.4", billing_provider="openai-codex",
        api_call_count=5, input_tokens=1000, output_tokens=200,
    )
    _insert_or_update_usage(
        temp_db, "sess-claude", "claude-3-7-sonnet", billing_provider="anthropic",
        api_call_count=10, input_tokens=5000, output_tokens=1000,
    )
    _insert_or_update_usage(
        temp_db, "sess-gemini", "gemini-2.5-pro", billing_provider="google",
        api_call_count=8, input_tokens=4000, output_tokens=800,
    )

    chk = capture_checkpoint("acct-filter", observed_at=1000.0, db_path=temp_db)
    # Only openai-codex row should be captured
    assert len(chk.entries) == 1
    assert ("sess-codex", "gpt-5.4", "") in chk.entries
    assert ("sess-claude", "claude-3-7-sonnet", "") not in chk.entries
    assert ("sess-gemini", "gemini-2.5-pro", "") not in chk.entries


def test_task_dimension_attribution(temp_db):
    """Test auxiliary task attribution (e.g. vision vs main loop)."""
    _insert_or_update_usage(
        temp_db, "sess-multi-task", "gpt-5.4", task="",
        api_call_count=5, input_tokens=1000, output_tokens=200,
    )
    _insert_or_update_usage(
        temp_db, "sess-multi-task", "gpt-5.4", task="vision",
        api_call_count=2, input_tokens=400, output_tokens=50,
    )

    chk_1 = capture_checkpoint("acct-task", observed_at=1000.0, db_path=temp_db)
    assert len(chk_1.entries) == 2
    assert ("sess-multi-task", "gpt-5.4", "") in chk_1.entries
    assert ("sess-multi-task", "gpt-5.4", "vision") in chk_1.entries

    _insert_or_update_usage(
        temp_db, "sess-multi-task", "gpt-5.4", task="vision",
        api_call_count=4, input_tokens=800, output_tokens=100,
    )
    chk_2 = capture_checkpoint("acct-task", observed_at=1100.0, db_path=temp_db)

    diff = derive_checkpoint_differences(chk_1, chk_2, db_path=temp_db)
    assert diff.status == "ok"
    d_main = next(d for d in diff.session_deltas if d.task == "")
    d_vision = next(d for d in diff.session_deltas if d.task == "vision")

    assert d_main.delta_counters.api_call_count == 0
    assert d_vision.delta_counters.api_call_count == 2
    assert d_vision.delta_counters.input_tokens == 400


def test_string_and_dict_endpoint_resolution(temp_db):
    """Test that derive_checkpoint_differences accepts string IDs and dicts."""
    _insert_or_update_usage(
        temp_db, "sess-1", "gpt-5.4", api_call_count=5,
        input_tokens=1000, output_tokens=200, observed_time=1000.0,
    )
    chk1 = capture_checkpoint("acct-endpoint", observed_at=1000.0, db_path=temp_db)

    _insert_or_update_usage(
        temp_db, "sess-1", "gpt-5.4", api_call_count=8,
        input_tokens=1500, output_tokens=300, observed_time=1100.0,
    )
    chk2 = capture_checkpoint("acct-endpoint", observed_at=1100.0, db_path=temp_db)

    # Pass by string IDs
    diff_str = derive_checkpoint_differences(chk1.checkpoint_id, chk2.checkpoint_id, db_path=temp_db)
    assert diff_str.status == "ok"
    assert diff_str.total_delta.api_call_count == 3

    # Pass by dict
    diff_dict = derive_checkpoint_differences(chk1.to_dict(), chk2.to_dict())
    assert diff_dict.status == "ok"
    assert diff_dict.total_delta.api_call_count == 3



def test_checkpoints_readonly_on_empty_db_no_ddl(tmp_path):
    """Calling get_checkpoint, get_latest_checkpoint, list_checkpoints on empty DB executes zero DDL."""
    db_path = tmp_path / "empty_chk.db"
    with sqlite3.connect(str(db_path)) as c:
        pass

    assert get_checkpoint("nonexistent", db_path=db_path) is None
    assert get_latest_checkpoint("acct-empty", db_path=db_path) is None
    assert list_checkpoints("acct-empty", db_path=db_path) == []

    with sqlite3.connect(str(db_path)) as c:
        tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert len(tables) == 0


def test_checkpoints_ro_connection_authorizer_denies_mutations(temp_db):
    """Authorizer on checkpoint readonly connection denies INSERT/UPDATE/DELETE/CREATE/ALTER/DROP."""
    from hermes_cli.codex_quota_snapshots import _connect_ro_db
    _insert_or_update_usage(
        temp_db, "sess-1", "gpt-5.4", api_call_count=1,
        input_tokens=100, output_tokens=50, observed_time=1000.0,
    )
    chk = capture_checkpoint("acct-auth", observed_at=1000.0, db_path=temp_db)
    assert chk is not None

    ro_conn = _connect_ro_db(temp_db)
    assert ro_conn is not None
    try:
        rows = ro_conn.execute("SELECT * FROM codex_activity_checkpoints").fetchall()
        assert len(rows) >= 1

        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro_conn.execute("INSERT INTO codex_activity_checkpoints (checkpoint_id, account_id, session_id, model, observed_at, created_at) VALUES ('c', 'a', 's', 'm', 1.0, 1.0)")

        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro_conn.execute("UPDATE codex_activity_checkpoints SET input_tokens = 999")

        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro_conn.execute("DELETE FROM codex_activity_checkpoints")

        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro_conn.execute("CREATE TABLE extra_table (id INT)")

        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro_conn.execute("ALTER TABLE codex_activity_checkpoints ADD COLUMN extra TEXT")

        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro_conn.execute("DROP TABLE codex_activity_checkpoints")
    finally:
        ro_conn.close()


def test_checkpoints_schema_and_data_before_after_identical(temp_db):
    """Calling checkpoint read helpers preserves identical schema and data before and after."""
    _insert_or_update_usage(
        temp_db, "sess-1", "gpt-5.4", api_call_count=5,
        input_tokens=500, output_tokens=100, observed_time=1000.0,
    )
    chk = capture_checkpoint("acct-ident", observed_at=1000.0, db_path=temp_db)

    def _state():
        with sqlite3.connect(str(temp_db)) as c:
            c.row_factory = sqlite3.Row
            schema = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
            data = c.execute("SELECT * FROM codex_activity_checkpoints ORDER BY checkpoint_id, session_id").fetchall()
            return [dict(r) for r in schema], [dict(r) for r in data]

    s_before, d_before = _state()

    assert get_checkpoint(chk.checkpoint_id, db_path=temp_db) is not None
    assert get_latest_checkpoint("acct-ident", db_path=temp_db) is not None
    assert len(list_checkpoints("acct-ident", db_path=temp_db)) >= 1

    s_after, d_after = _state()
    assert s_before == s_after
    assert d_before == d_after


def test_prune_activity_checkpoints_on_empty_db_no_init(tmp_path):
    """prune_activity_checkpoints on empty DB does not initialize table."""
    db_path = tmp_path / "empty_prune_act.db"
    with sqlite3.connect(str(db_path)) as c:
        pass

    assert prune_activity_checkpoints(db_path=db_path) == 0

    with sqlite3.connect(str(db_path)) as c:
        tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert len(tables) == 0


def test_checkpoints_missing_db_returns_none_and_does_not_create(tmp_path):
    """Calling checkpoint read helpers on nonexistent DB returns None/[] without creating file."""
    missing = tmp_path / "no_such_dir" / "missing.db"
    assert not missing.exists()

    assert get_checkpoint("chk_1", db_path=missing) is None
    assert get_latest_checkpoint("acct-1", db_path=missing) is None
    assert list_checkpoints("acct-1", db_path=missing) == []
    assert not missing.exists()
