import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from hermes_cli.auth import (
    init_gemini_quota_snapshots_table,
    record_gemini_quota_snapshots,
    get_gemini_quota_timeline,
    get_account_alias,
)
from hermes_cli import web_server


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provide an isolated state.db path."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("hermes_cli.auth.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user1@example.com": "alias1",
                    "user2@example.com": "alias2",
                    "user3@example.com": "alias3",
                    "user4@example.com": "alias4",
                    "user5@example.com": "alias5",
                    "u1@example.com": "alias1",
                    "u2@example.com": "alias2",
                    "u3@example.com": "alias3",
                    "u4@example.com": "alias4",
                    "u5@example.com": "alias5",
                    "acc1@test.com": "alias1",
                    "acc2@test.com": "alias2",
                    "acc3@test.com": "alias3",
                    "acc4@test.com": "alias4",
                    "acc5@test.com": "alias5",
                }
            }
        },
    )
    return db_path


def test_init_gemini_quota_snapshots_table(temp_db):
    """Verify table creation with the exact schema columns and indexes."""
    conn = sqlite3.connect(str(temp_db))
    init_gemini_quota_snapshots_table(conn)

    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(gemini_quota_snapshots)")
    cols = {row[1]: row[2] for row in cursor.fetchall()}

    expected_cols = {
        "id": "INTEGER",
        "timestamp": "REAL",
        "time_label": "TEXT",
        "account_id": "INTEGER",
        "alias": "TEXT",
        "email": "TEXT",
        "gemini_5h_percent": "REAL",
        "gemini_5h_reset": "TEXT",
        "gemini_weekly_percent": "REAL",
        "gemini_weekly_reset": "TEXT",
        "gemini_doci_score": "REAL",
        "gemini_rank": "INTEGER",
        "claude_5h_percent": "REAL",
        "claude_5h_reset": "TEXT",
        "claude_weekly_percent": "REAL",
        "claude_weekly_reset": "TEXT",
        "claude_doci_score": "REAL",
        "claude_rank": "INTEGER",
        "doci_score": "REAL",
        "rank": "INTEGER",
    }
    for col_name, col_type in expected_cols.items():
        assert col_name in cols, f"Missing column: {col_name}"

    # Verify unique index
    cursor.execute("PRAGMA index_list(gemini_quota_snapshots)")
    indexes = [row[1] for row in cursor.fetchall()]
    assert "idx_gemini_quota_snapshots_slot_acc" in indexes
    conn.close()


def test_record_gemini_quota_snapshots(temp_db, monkeypatch):
    """Verify taking snapshot persists all 5 accounts with correct metrics and aliases."""
    mock_status = {
        "logged_in": True,
        "accounts": [
            {
                "account_id": 1,
                "email": "user1@example.com",
                "logged_in": True,
                "quota": {
                    "gemini_5h_percent": 90.0,
                    "gemini_weekly_percent": 95.0,
                    "claude_5h_percent": 80.0,
                    "claude_weekly_percent": 85.0,
                },
            },
            {
                "account_id": 2,
                "email": "user2@example.com",
                "logged_in": True,
                "quota": {
                    "gemini_5h_percent": 75.0,
                    "gemini_weekly_percent": 80.0,
                    "claude_5h_percent": 70.0,
                    "claude_weekly_percent": 75.0,
                },
            },
            {
                "account_id": 3,
                "email": "user3@example.com",
                "logged_in": True,
                "quota": {
                    "gemini_5h_percent": 60.0,
                    "gemini_weekly_percent": 65.0,
                    "claude_5h_percent": 50.0,
                    "claude_weekly_percent": 55.0,
                },
            },
            {
                "account_id": 4,
                "email": "user4@example.com",
                "logged_in": True,
                "quota": {
                    "gemini_5h_percent": 45.0,
                    "gemini_weekly_percent": 50.0,
                    "claude_5h_percent": 40.0,
                    "claude_weekly_percent": 45.0,
                },
            },
            {
                "account_id": 5,
                "email": "user5@example.com",
                "logged_in": False,
                "quota": {},
            },
        ],
        "doci_rankings": [
            {"account_id": 1, "rank": 1, "doci_score": 0.95},
            {"account_id": 2, "rank": 2, "doci_score": 0.85},
            {"account_id": 3, "rank": 3, "doci_score": 0.65},
            {"account_id": 4, "rank": 4, "doci_score": 0.45},
            {"account_id": 5, "rank": 5, "doci_score": 0.0},
        ],
    }

    monkeypatch.setattr("hermes_cli.auth.get_all_gemini_accounts_status", lambda **kw: mock_status)

    slot_epoch = 1756123200.0  # 15m aligned timestamp
    count = record_gemini_quota_snapshots(db_path=temp_db, slot_epoch=slot_epoch, force_refresh=False)
    assert count == 5

    conn = sqlite3.connect(str(temp_db))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM gemini_quota_snapshots WHERE timestamp = ? ORDER BY account_id ASC", (slot_epoch,))
    rows = cursor.fetchall()
    assert len(rows) == 5

    aliases = [r["alias"] for r in rows]
    assert aliases == ["alias1", "alias2", "alias3", "alias4", "alias5"]

    assert rows[0]["gemini_5h_percent"] == 90.0
    assert rows[0]["gemini_weekly_percent"] == 95.0
    assert rows[0]["rank"] == 1
    assert rows[4]["gemini_5h_percent"] == 0.0
    assert rows[4]["rank"] == 5

    # Test ON CONFLICT update on same slot
    mock_status["accounts"][0]["quota"]["gemini_5h_percent"] = 88.0
    record_gemini_quota_snapshots(db_path=temp_db, slot_epoch=slot_epoch, force_refresh=False)

    cursor.execute("SELECT gemini_5h_percent FROM gemini_quota_snapshots WHERE timestamp = ? AND account_id = 1", (slot_epoch,))
    updated_val = cursor.fetchone()[0]
    assert updated_val == 88.0
    conn.close()


def test_get_gemini_quota_timeline_24h_96_intervals(temp_db, monkeypatch):
    """Verify 24h timespan produces exactly 96 intervals covering the last 24 hours."""
    mock_status = {
        "logged_in": True,
        "accounts": [
            {"account_id": idx, "email": f"u{idx}@example.com", "logged_in": True, "quota": {"gemini_5h_percent": 80.0, "gemini_weekly_percent": 90.0}}
            for idx in range(1, 6)
        ],
        "doci_rankings": [{"account_id": idx, "rank": idx, "doci_score": 1.0 - idx * 0.1} for idx in range(1, 6)],
    }
    monkeypatch.setattr("hermes_cli.auth.get_all_gemini_accounts_status", lambda **kw: mock_status)

    res = get_gemini_quota_timeline(timespan="24h", model_group="gemini")
    assert res["timespan"] == "24h"
    assert res["model_group"] == "gemini"
    assert res["interval_minutes"] == 15
    assert res["total_intervals"] == 96
    assert len(res["intervals"]) == 96
    assert len(res["accounts_meta"]) == 5

    # Latest interval is marked is_current
    assert res["intervals"][-1]["is_current"] is True
    assert res["intervals"][0]["is_current"] is False

    # Check 5 account aliases in metadata
    meta_aliases = [a["alias"] for a in res["accounts_meta"]]
    assert meta_aliases == ["alias1", "alias2", "alias3", "alias4", "alias5"]


def test_get_gemini_quota_timeline_timespans(temp_db, monkeypatch):
    """Verify timespan calculations: 48h=192, 72h=288, 7d=672 intervals."""
    mock_status = {
        "logged_in": True,
        "accounts": [{"account_id": idx, "logged_in": False, "quota": {}} for idx in range(1, 6)],
        "doci_rankings": [],
    }
    monkeypatch.setattr("hermes_cli.auth.get_all_gemini_accounts_status", lambda **kw: mock_status)

    res_48h = get_gemini_quota_timeline(timespan="48h")
    assert res_48h["total_intervals"] == 192
    assert len(res_48h["intervals"]) == 192

    res_72h = get_gemini_quota_timeline(timespan="72h")
    assert res_72h["total_intervals"] == 288
    assert len(res_72h["intervals"]) == 288

    res_7d = get_gemini_quota_timeline(timespan="7d")
    assert res_7d["total_intervals"] == 672
    assert len(res_7d["intervals"]) == 672


def test_get_gemini_quota_timeline_ground_truth_nulls(temp_db, monkeypatch):
    """Verify that unrecorded historical slots return None/null with zero synthetic fake data."""
    mock_status = {
        "logged_in": True,
        "accounts": [
            {"account_id": 1, "email": "u1@example.com", "logged_in": True, "quota": {"gemini_5h_percent": 90.0, "gemini_weekly_percent": 95.0}},
            {"account_id": 2, "email": "u2@example.com", "logged_in": True, "quota": {"gemini_5h_percent": 80.0, "gemini_weekly_percent": 85.0}},
            {"account_id": 3, "email": "u3@example.com", "logged_in": True, "quota": {"gemini_5h_percent": 70.0, "gemini_weekly_percent": 75.0}},
            {"account_id": 4, "email": "u4@example.com", "logged_in": True, "quota": {"gemini_5h_percent": 60.0, "gemini_weekly_percent": 65.0}},
            {"account_id": 5, "email": "u5@example.com", "logged_in": True, "quota": {"gemini_5h_percent": 50.0, "gemini_weekly_percent": 55.0}},
        ],
        "doci_rankings": [{"account_id": idx, "rank": idx, "doci_score": 1.0 - idx * 0.1} for idx in range(1, 6)],
    }
    monkeypatch.setattr("hermes_cli.auth.get_all_gemini_accounts_status", lambda **kw: mock_status)

    res = get_gemini_quota_timeline(timespan="24h", model_group="gemini")
    intervals = res["intervals"]

    # Current slot was recorded on-demand, so it has values
    current_slot = intervals[-1]
    assert current_slot["is_current"] is True
    assert current_slot["accounts"]["alias1"]["cap_5h"] == 90.0
    assert current_slot["accounts"]["alias1"]["rank"] is not None

    # Historical slots before snapshot daemon ran are unrecorded -> None / null
    past_slot = intervals[0]
    assert past_slot["is_current"] is False
    assert past_slot["accounts"]["alias1"]["cap_5h"] is None
    assert past_slot["accounts"]["alias1"]["cap_7d"] is None
    assert past_slot["accounts"]["alias1"]["rank"] is None
    assert past_slot["accounts"]["alias1"]["score"] is None


def test_get_gemini_quota_timeline_claude_model_group(temp_db, monkeypatch):
    """Verify switching to Claude model group returns Claude quota columns."""
    mock_status = {
        "logged_in": True,
        "accounts": [
            {
                "account_id": 1,
                "email": "u1@example.com",
                "logged_in": True,
                "quota": {
                    "gemini_5h_percent": 90.0,
                    "gemini_weekly_percent": 95.0,
                    "claude_5h_percent": 42.0,
                    "claude_weekly_percent": 68.0,
                },
            },
            {"account_id": 2, "logged_in": False, "quota": {}},
            {"account_id": 3, "logged_in": False, "quota": {}},
            {"account_id": 4, "logged_in": False, "quota": {}},
            {"account_id": 5, "logged_in": False, "quota": {}},
        ],
        "doci_rankings": [{"account_id": 1, "rank": 1, "doci_score": 0.88}],
    }
    monkeypatch.setattr("hermes_cli.auth.get_all_gemini_accounts_status", lambda **kw: mock_status)

    res = get_gemini_quota_timeline(timespan="24h", model_group="claude")
    assert res["model_group"] == "claude"
    current_slot = res["intervals"][-1]
    assert current_slot["accounts"]["alias1"]["cap_5h"] == 42.0
    assert current_slot["accounts"]["alias1"]["cap_7d"] == 68.0


def test_gemini_quota_timeline_web_endpoint(temp_db, monkeypatch):
    """Verify GET /api/gemini/quota-timeline endpoint via FastAPI TestClient."""
    mock_status = {
        "logged_in": True,
        "accounts": [
            {"account_id": idx, "email": f"user{idx}@test.com", "logged_in": True, "quota": {"gemini_5h_percent": 85.0, "gemini_weekly_percent": 90.0}}
            for idx in range(1, 6)
        ],
        "doci_rankings": [{"account_id": idx, "rank": idx, "doci_score": 0.9} for idx in range(1, 6)],
    }
    monkeypatch.setattr("hermes_cli.auth.get_all_gemini_accounts_status", lambda **kw: mock_status)

    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = None
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

    resp = client.get("/api/gemini/quota-timeline?timespan=24h&model_group=gemini")
    assert resp.status_code == 200
    data = resp.json()
    assert data["timespan"] == "24h"
    assert data["total_intervals"] == 96
    assert len(data["intervals"]) == 96
    assert len(data["accounts_meta"]) == 5

    # Check alias endpoint /api/gemini/timeline
    resp_alias = client.get("/api/gemini/timeline?timespan=48h")
    assert resp_alias.status_code == 200
    assert resp_alias.json()["total_intervals"] == 192


def test_gemini_quota_timeline_custom_alias_override(temp_db, monkeypatch):
    """Verify display.account_aliases config override is honored."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "primary@company.com": "custom_primary",
                    "gemini-2": "custom_secondary",
                }
            }
        },
    )
    mock_status = {
        "logged_in": True,
        "accounts": [
            {"account_id": 1, "email": "primary@company.com", "logged_in": True, "quota": {"gemini_5h_percent": 95.0, "gemini_weekly_percent": 99.0}},
            {"account_id": 2, "email": "", "logged_in": True, "quota": {"gemini_5h_percent": 80.0, "gemini_weekly_percent": 85.0}},
            {"account_id": 3, "email": "", "logged_in": False, "quota": {}},
            {"account_id": 4, "email": "", "logged_in": False, "quota": {}},
            {"account_id": 5, "email": "", "logged_in": False, "quota": {}},
        ],
        "doci_rankings": [],
    }
    monkeypatch.setattr("hermes_cli.auth.get_all_gemini_accounts_status", lambda **kw: mock_status)

    res = get_gemini_quota_timeline(timespan="24h")
    meta_aliases = [a["alias"] for a in res["accounts_meta"]]
    assert meta_aliases[0] == "custom_primary"
    assert meta_aliases[1] == "custom_secondary"
    assert meta_aliases[2] == "gemini-3"  # unaliased fallback


def test_gemini_quota_timeline_multiple_stored_slots(temp_db, monkeypatch):
    """Verify multiple historical snapshots stored in state.db are accurately returned."""
    mock_status = {
        "logged_in": True,
        "accounts": [
            {"account_id": idx, "email": f"acc{idx}@test.com", "logged_in": True, "quota": {"gemini_5h_percent": 100.0 - idx * 10, "gemini_weekly_percent": 90.0}}
            for idx in range(1, 6)
        ],
        "doci_rankings": [{"account_id": idx, "rank": idx, "doci_score": 1.0 - idx * 0.1} for idx in range(1, 6)],
    }
    monkeypatch.setattr("hermes_cli.auth.get_all_gemini_accounts_status", lambda **kw: mock_status)

    now_ts = time.time()
    cur_slot = int(now_ts // 900) * 900
    prev_slot_1 = cur_slot - 900
    prev_slot_2 = cur_slot - 1800

    # Record 3 distinct snapshot slots
    record_gemini_quota_snapshots(db_path=temp_db, slot_epoch=prev_slot_2)
    record_gemini_quota_snapshots(db_path=temp_db, slot_epoch=prev_slot_1)
    record_gemini_quota_snapshots(db_path=temp_db, slot_epoch=cur_slot)

    res = get_gemini_quota_timeline(timespan="24h")
    intervals = res["intervals"]

    # Slot 95 is cur_slot
    assert intervals[-1]["epoch"] == cur_slot
    assert intervals[-1]["accounts"]["alias1"]["cap_5h"] == 90.0
    assert intervals[-1]["accounts"]["alias1"]["rank"] == 1

    # Slot 94 is prev_slot_1
    assert intervals[-2]["epoch"] == prev_slot_1
    assert intervals[-2]["accounts"]["alias1"]["cap_5h"] == 90.0

    # Slot 93 is prev_slot_2
    assert intervals[-3]["epoch"] == prev_slot_2
    assert intervals[-3]["accounts"]["alias1"]["cap_5h"] == 90.0

    # Slot 0 (24h ago) is unrecorded
    assert intervals[0]["accounts"]["alias1"]["cap_5h"] is None
    assert intervals[0]["accounts"]["alias1"]["rank"] is None


def test_get_gemini_session_histories_is_sync_def():
    """Verify endpoint is synchronous so FastAPI runs it on threadpool, avoiding event loop stalls."""
    import inspect
    from hermes_cli.web_routers.gemini import get_gemini_session_histories

    assert not inspect.iscoroutinefunction(get_gemini_session_histories), (
        "get_gemini_session_histories must NOT be async def to prevent freezing FastAPI event loop"
    )


def test_get_account_alias_caching(monkeypatch):
    """Verify get_account_alias caches config loads and performs fast lookups."""
    import hermes_cli.auth as auth_mod
    from hermes_cli.auth import get_account_alias

    # Reset cache
    auth_mod._CONFIG_ALIASES_CACHE = (0.0, {})

    call_count = 0

    def mock_load():
        nonlocal call_count
        call_count += 1
        return {"display": {"account_aliases": {"user1@test.com": "AliasOne"}}}

    monkeypatch.setattr("hermes_cli.config.load_config", mock_load)

    # 100 consecutive calls should hit cache and only invoke load_config once
    for _ in range(100):
        alias = get_account_alias("user1@test.com")
        assert alias == "AliasOne"

    assert call_count == 1
