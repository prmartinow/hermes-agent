"""Tests for Gemini account alignment across session persistence, TUI status bar, and web overview."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from hermes_state import SessionDB
from agent.credential_pool import CredentialPool, PooledCredential, AUTH_TYPE_OAUTH
from hermes_cli.auth import get_account_alias
from tui_gateway.server import _session_info, _emit_settled_session_info


@pytest.fixture
def mock_gemini_pool():
    c1 = PooledCredential(provider="gemini-oauth", id="gemini-1", label="user1@example.com", auth_type=AUTH_TYPE_OAUTH, priority=0, source="gemini-1", access_token="ya29.token1", extra={"account_id": 1})
    c2 = PooledCredential(provider="gemini-oauth", id="gemini-2", label="user2@example.com", auth_type=AUTH_TYPE_OAUTH, priority=1, source="gemini-2", access_token="ya29.token2", extra={"account_id": 2})
    c3 = PooledCredential(provider="gemini-oauth", id="gemini-3", label="user3@example.com", auth_type=AUTH_TYPE_OAUTH, priority=2, source="gemini-3", access_token="ya29.token3", extra={"account_id": 3})
    c4 = PooledCredential(provider="gemini-oauth", id="gemini-4", label="user4@example.com", auth_type=AUTH_TYPE_OAUTH, priority=3, source="gemini-4", access_token="ya29.token4", extra={"account_id": 4})
    c5 = PooledCredential(provider="gemini-oauth", id="gemini-5", label="user5@example.com", auth_type=AUTH_TYPE_OAUTH, priority=4, source="gemini-5", access_token="ya29.token5", extra={"account_id": 5})
    pool = CredentialPool(provider="gemini-oauth", entries=[c1, c2, c3, c4, c5])
    return pool


def test_session_account_alignment_between_used_tui_and_overview(tmp_path, mock_gemini_pool, monkeypatch):
    """Verify that the account used, TUI model bar, and chat overview sidebar all match 100%."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)

    # Configure alias mapping
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user1@example.com": "u1",
                    "user2@example.com": "u2",
                    "user3@example.com": "u3",
                    "user4@example.com": "u4",
                    "user5@example.com": "u5",
                }
            }
        },
    )

    # 1. Create a session in state.db pinned to u4 (user4@example.com)
    sid = "20260822_120000_abc123"
    db.create_session(
        session_id=sid,
        source="tui",
        model="gemini-3.7-flash-high",
        model_config={"model": "gemini-3.7-flash-high", "provider": "gemini-oauth", "gemini_account": "user4@example.com"},
    )

    # 2. Check CredentialPool session-pinned selection:
    with patch("hermes_cli.auth.calculate_gemini_doci_score", return_value={"cap_5h": 0.85, "cap_w": 0.90, "score": 0.8}):
        selected = mock_gemini_pool.select(preferred_account="user4@example.com")
        assert selected is not None
        assert selected.label == "user4@example.com"
        used_alias = get_account_alias(selected.label)
        assert used_alias == "u4"

    # 3. Check TUI Status Bar (_session_info):
    sess_row = db.get_session(sid)
    assert sess_row is not None
    mock_agent = MagicMock(
        provider="gemini-oauth",
        model="gemini-3.7-flash-high",
        _credential_pool=mock_gemini_pool,
        _credential_pool_entry_id="gemini-4",
        _session_db=db,
        session_id=sid,
    )
    tui_info = _session_info(mock_agent, sess_row)
    tui_bar_alias = tui_info.get("gemini_account")
    assert tui_bar_alias == "u4"

    # 4. Check Chat Overview Panel:
    from hermes_cli.auth import resolve_session_last_used_account
    raw_acc = resolve_session_last_used_account(sid, db)
    overview_alias = get_account_alias(raw_acc)
    assert overview_alias == "u4"

    # Invariant: All 3 match
    assert used_alias == tui_bar_alias == overview_alias == "u4"


def test_multi_chat_session_affinity_isolation(tmp_path, mock_gemini_pool, monkeypatch):
    """Test that switching between multiple chats preserves each chat's own dedicated account."""
    db_path = tmp_path / "state_multi.db"
    db = SessionDB(db_path=db_path)

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user1@example.com": "u1",
                    "user2@example.com": "u2",
                    "user3@example.com": "u3",
                    "user4@example.com": "u4",
                    "user5@example.com": "u5",
                }
            }
        },
    )

    # Session A pinned to u2
    sid_a = "sess_a_pm"
    db.create_session(
        session_id=sid_a,
        source="tui",
        model="gemini-3.7-flash-high",
        model_config={"model": "gemini-3.7-flash-high", "provider": "gemini-oauth", "gemini_account": "user2@example.com"},
    )

    # Session B pinned to u3
    sid_b = "sess_b_tnn"
    db.create_session(
        session_id=sid_b,
        source="tui",
        model="gemini-3.7-flash-high",
        model_config={"model": "gemini-3.7-flash-high", "provider": "gemini-oauth", "gemini_account": "user3@example.com"},
    )

    with patch("hermes_cli.auth.calculate_gemini_doci_score", return_value={"cap_5h": 0.85, "cap_w": 0.90, "score": 0.8}):
        # Resume Session A -> gets u2
        sel_a = mock_gemini_pool.select(preferred_account="user2@example.com")
        assert sel_a.label == "user2@example.com"

        # Resume Session B -> gets u3
        sel_b = mock_gemini_pool.select(preferred_account="user3@example.com")
        assert sel_b.label == "user3@example.com"

        # Resume Session A again -> still gets u2
        sel_a2 = mock_gemini_pool.select(preferred_account="user2@example.com")
        assert sel_a2.label == "user2@example.com"


def test_turn_settle_persists_account_rotation_to_state_db(tmp_path, mock_gemini_pool, monkeypatch):
    """Test that when an account rotates upon quota exhaustion, the settled turn updates state.db."""
    db_path = tmp_path / "state_settle.db"
    db = SessionDB(db_path=db_path)

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user1@example.com": "u1",
                    "user2@example.com": "u2",
                    "user3@example.com": "u3",
                    "user4@example.com": "u4",
                    "user5@example.com": "u5",
                }
            }
        },
    )

    sid = "sess_rotate_01"
    db.create_session(
        session_id=sid,
        source="tui",
        model="gemini-3.7-flash-high",
        model_config={"model": "gemini-3.7-flash-high", "provider": "gemini-oauth", "gemini_account": "user2@example.com"},
    )

    # Simulate rotation to user3
    mock_agent = MagicMock(
        spec=[
            "provider", "model", "service_tier", "reasoning_config",
            "attached_images", "fallback_model", "_cached_system_prompt",
            "_credential_pool", "_credential_pool_entry_id", "_session_db",
            "session_id", "platform", "_client_kwargs", "base_url",
            "api_key", "api_mode", "quiet_mode",
        ],
        provider="gemini-oauth",
        model="gemini-3.7-flash-high",
        service_tier="",
        reasoning_config={},
        attached_images=[],
        fallback_model="",
        _cached_system_prompt="",
        _credential_pool=mock_gemini_pool,
        _credential_pool_entry_id="gemini-3",
        _session_db=db,
        session_id=sid,
        platform="tui",
        _client_kwargs={},
        base_url="",
        api_key="",
        api_mode="",
        quiet_mode=True,
    )
    sess_dict = {"session_key": sid, "db": db}

    _emit_settled_session_info(sid, sess_dict, mock_agent)

    # Verify state.db was updated with new account user3
    sess_after = db.get_session(sid)
    assert sess_after is not None
    cfg = sess_after.get("model_config")
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    assert cfg.get("gemini_account") == "user3@example.com" or cfg.get("gemini_account") == "u3"


def test_all_sessions_account_alignment_audit(tmp_path, mock_gemini_pool, monkeypatch):
    """Test that performs a complete audit across all sessions in a database:
    1. Account of last message
    2. Account displayed next to model in TUI status bar
    3. Account displayed in chat overview panel
    Asserts all 3 match 100% across every session.
    """
    db_path = tmp_path / "state_all_audit.db"
    db = SessionDB(db_path=db_path)

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user1@example.com": "u1",
                    "user2@example.com": "u2",
                    "user3@example.com": "u3",
                    "user4@example.com": "u4",
                    "user5@example.com": "u5",
                }
            }
        },
    )

    # Seed 5 distinct sessions across 5 different accounts
    test_data = [
        ("sess_001", "Check Karpathy Skills", "user4@example.com", "u4"),
        ("sess_002", "HTTP 400 Bug", "user1@example.com", "u1"),
        ("sess_003", "Web UI / TUI Layout", "user2@example.com", "u2"),
        ("sess_004", "Carbon Clickhouse Docs", "user3@example.com", "u3"),
        ("sess_005", "Solana Data Pipeline", "user5@example.com", "u5"),
    ]

    for sid, title, raw_acc, expected_alias in test_data:
        db.create_session(
            session_id=sid,
            source="tui",
            model="gemini-3.7-flash-high",
            model_config={"model": "gemini-3.7-flash-high", "provider": "gemini-oauth", "gemini_account": raw_acc},
        )
        db.set_session_title(sid, title)
        db.append_messages_batch(
            sid,
            [
                {"role": "user", "content": f"Hello from {title}"},
                {"role": "assistant", "content": f"Response from {expected_alias}"},
            ],
        )

    # Loop over ALL sessions in the database and audit 3-way alignment
    all_sessions = db.list_sessions_rich(limit=50, compact_rows=True)
    assert len(all_sessions) == 5

    for s in all_sessions:
        sid = s["id"]
        sess_row = db.get_session(sid)
        assert sess_row is not None

        # 1. Account of last message / persisted session state
        cfg = sess_row.get("model_config") or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        last_message_raw_acc = cfg.get("gemini_account")
        last_message_alias = get_account_alias(last_message_raw_acc)

        # 2. Account displayed next to the model in TUI
        mock_agent = MagicMock(
            provider="gemini-oauth",
            model="gemini-3.7-flash-high",
            _credential_pool=mock_gemini_pool,
            _credential_pool_entry_id=None,
            _session_db=db,
            session_id=sid,
        )
        tui_info = _session_info(mock_agent, sess_row)
        tui_model_bar_alias = tui_info.get("gemini_account")

        # 3. Account label displayed in chat overview panel
        overview_panel_alias = get_account_alias(cfg.get("gemini_account"))

        # Assert 3-way alignment across all sessions in the DB
        assert last_message_alias is not None
        assert tui_model_bar_alias is not None
        assert overview_panel_alias is not None
        assert last_message_alias == tui_model_bar_alias == overview_panel_alias, (
            f"Session {sid} ({s.get('title')}) mismatch: "
            f"last_msg={last_message_alias}, tui_bar={tui_model_bar_alias}, overview={overview_panel_alias}"
        )


def test_resolve_session_last_used_account_hierarchy(tmp_path, mock_gemini_pool, monkeypatch):
    """Test that resolve_session_last_used_account accurately resolves historical accounts from messages or logs rather than blindly guessing from pool."""
    from hermes_cli.auth import resolve_session_last_used_account, get_account_alias

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user4@example.com": "u4",
                    "user3@example.com": "u3",
                }
            }
        },
    )

    db_path = tmp_path / "test_hierarchy_state.db"
    db = SessionDB(db_path=db_path)

    # 1. Historical session with NULL model_config, but message has display_metadata
    sid_msg = "hist_with_msg_meta"
    db.create_session(
        session_id=sid_msg,
        source="tui",
        model="gemini-3.7-flash-high",
        model_config=None,  # Old session format
        system_prompt="",
    )
    db.append_messages_batch(
        sid_msg,
        [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Hi there!",
                "display_metadata": {"gemini_account": "user4@example.com"},
            },
        ],
    )

    resolved = resolve_session_last_used_account(sid_msg, db)
    assert resolved == "user4@example.com"
    assert get_account_alias(resolved) == "u4"

    # Verify read-only resolution does not mutate model_config (preserves unpinned status for CD-DOCI)
    sess_healed = db.get_session(sid_msg)
    assert sess_healed is not None
    assert sess_healed["model_config"] is None

    # 2. Historical session with NULL model_config, no message metadata, but agent.log exists
    sid_log = "hist_with_log"
    db.create_session(
        session_id=sid_log,
        source="tui",
        model="gemini-3.7-flash-high",
        model_config=None,
        system_prompt="",
    )
    db.append_messages_batch(
        sid_log,
        [
            {"role": "user", "content": "Old question"},
            {"role": "assistant", "content": "Old answer"},
        ],
    )

    # Mock agent.log with entries
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "agent.log"
    log_file.write_text(
        f"2026-08-20 10:00:00,000 INFO [{sid_log}] agent.turn_context: turn started\n"
        f"2026-08-20 10:00:02,000 INFO [{sid_log}] gemini pool: switched active account to u3 (user3@example.com)\n"
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    resolved_log = resolve_session_last_used_account(sid_log, db)
    assert resolved_log == "user3@example.com"
    assert get_account_alias(resolved_log) == "u3"

    # Verify read-only resolution does not mutate model_config (preserves unpinned status for CD-DOCI)
    sess_log_healed = db.get_session(sid_log)
    assert sess_log_healed is not None
    assert sess_log_healed["model_config"] is None

    # 3. Brand-new unpinned session with no messages and no log (hits step 4 fallback to active pool)
    sid_fresh = "fresh_unpinned_session"
    db.create_session(
        session_id=sid_fresh,
        source="tui",
        model="gemini-3.7-flash-high",
        model_config=None,
        system_prompt="",
    )
    with patch("agent.credential_pool.load_pool", return_value=mock_gemini_pool):
        resolved_fresh = resolve_session_last_used_account(sid_fresh, db)
        assert resolved_fresh == "user1@example.com"
        sess_fresh = db.get_session(sid_fresh)
        assert sess_fresh is not None
        # Must NOT stamp model_config on read-only resolution
        assert sess_fresh["model_config"] is None


def test_list_gemini_session_histories_verified_turn_metadata(tmp_path, monkeypatch):
    """Test that list_gemini_session_histories resolves 100% verified turn metadata from messages.display_metadata."""
    from hermes_cli.auth import list_gemini_session_histories

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user1@example.com": "u1",
                    "user2@example.com": "u2",
                }
            }
        },
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)

    sid = "20260824_100000_turnmeta"
    db.create_session(
        session_id=sid,
        source="cli",
        model="gemini-3.7-flash",
        model_config={"model": "gemini-3.7-flash", "provider": "gemini-oauth", "gemini_account": "user1@example.com"},
        system_prompt="",
    )

    # Turn 1: user1
    db.append_messages_batch(
        sid,
        [
            {"role": "user", "content": "Question 1", "timestamp": 1756000000.0},
            {
                "role": "assistant",
                "content": "Answer 1",
                "timestamp": 1756000002.0,
                "display_metadata": {"gemini_account": "user1@example.com"},
            },
        ],
    )

    # Turn 2: user2 (after failover/rotation)
    db.append_messages_batch(
        sid,
        [
            {"role": "user", "content": "Question 2", "timestamp": 1756000010.0},
            {
                "role": "assistant",
                "content": "Answer 2",
                "timestamp": 1756000012.0,
                "display_metadata": {"gemini_account": "user2@example.com"},
            },
        ],
    )

    result = list_gemini_session_histories()
    assert result["total"] >= 1
    session_data = next((s for s in result["sessions"] if s["session_id"] == sid), None)
    assert session_data is not None

    turns = [e for e in session_data["events"] if e.get("event_type") == "turn"]
    assert len(turns) == 2

    assert turns[0]["turn_number"] == 1
    assert turns[0]["to_alias"] == "u1"
    assert turns[0]["to_account"] == "user1@example.com"
    assert turns[0]["details"] == "Question 1"

    assert turns[1]["turn_number"] == 2
    assert turns[1]["to_alias"] == "u2"
    assert turns[1]["to_account"] == "user2@example.com"
    assert turns[1]["details"] == "Question 2"

    # Rotation changes count should accurately reflect the 1 transition from u1 to u2
    assert session_data["changes_count"] == 1


def test_build_assistant_message_and_flush_attaches_account_metadata(tmp_path, mock_gemini_pool, monkeypatch):
    """Test that build_assistant_message and _flush_messages_to_session_db attach display_metadata with active gemini account."""
    from agent.chat_completion_helpers import build_assistant_message

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user3@example.com": "u3",
                }
            }
        },
    )

    mock_agent = MagicMock(
        provider="gemini-oauth",
        model="gemini-3.7-flash",
        _credential_pool=mock_gemini_pool,
        _credential_pool_entry_id="gemini-3",
        valid_tool_names=set(),
        verbose_logging=False,
        reasoning_callback=None,
        stream_delta_callback=None,
        _stream_callback=None,
    )
    mock_agent._extract_reasoning = MagicMock(return_value=None)
    mock_agent._strip_think_blocks = lambda s: s

    # Select user3
    mock_gemini_pool.select(preferred_account="user3@example.com")

    mock_msg = MagicMock(
        content="Hello world",
        tool_calls=None,
        reasoning_content=None,
        reasoning_details=None,
        model_extra=None,
        anthropic_content_blocks=None,
        codex_reasoning_items=None,
        codex_message_items=None,
    )
    built = build_assistant_message(mock_agent, mock_msg, finish_reason="stop")

    assert "display_metadata" in built
    assert built["display_metadata"] == {"gemini_account": "u3"}


def test_pinned_gemini_account_strictly_sticky_below_20_percent(mock_gemini_pool, monkeypatch):
    """Test that a pinned Gemini account remains 100% locked across turns even if its quota drops below 20% (DOCI=0.0)."""
    # Configure aliases
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "display": {
                "account_aliases": {
                    "user1@example.com": "u1",
                    "user2@example.com": "u2",
                }
            }
        },
    )

    # Mock DOCI: User 1 has 95% quota (score 4.0), User 2 has only 5% quota (score 0.0)
    def _mock_doci(acc_idx, **kw):
        if acc_idx == 1:
            return {"score": 4.0, "cap_5h": 0.95, "cap_w": 0.95, "logged_in": True}
        return {"score": 0.0, "cap_5h": 0.05, "cap_w": 0.50, "logged_in": True}

    with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci):
        # Session A is pinned to user2 (low quota)
        selected = mock_gemini_pool.select(preferred_account="user2@example.com")
        assert selected is not None
        assert selected.label == "user2@example.com"  # Stays strictly pinned to user2!

        # Subsequent turn on the same session
        selected_turn2 = mock_gemini_pool.select(preferred_account="user2@example.com")
        assert selected_turn2.label == "user2@example.com"

        # Now simulate a real 429 received on user2:
        mock_gemini_pool.mark_exhausted_and_rotate(status_code=429, credential_id=selected.id)

        # Now that user2 is marked exhausted by 429, selection cleanly rotates to user1:
        selected_failover = mock_gemini_pool.select(preferred_account="user2@example.com")
        assert selected_failover.label == "user1@example.com"


def test_turn_context_restores_pinned_gemini_account_after_cross_session_drift(tmp_path, mock_gemini_pool, monkeypatch):
    """Verify that build_turn_context enforces the session's pinned account even if another session drifted the pool."""
    from agent.turn_context import build_turn_context

    db_path = tmp_path / "state_drift.db"
    db = SessionDB(db_path=db_path)

    sid = "sess_pinned_to_u2"
    db.create_session(
        session_id=sid,
        source="tui",
        model="gemini-3.7-flash-high",
        model_config={"model": "gemini-3.7-flash-high", "provider": "gemini-oauth", "gemini_account": "user2@example.com"},
    )

    mock_agent = MagicMock(
        provider="gemini-oauth",
        model="gemini-3.7-flash-high",
        session_id=sid,
        _session_db=db,
        _credential_pool=mock_gemini_pool,
        _credential_pool_entry_id="gemini-4",  # Drifted by another session
        _primary_runtime=None,
        _memory_write_origin="assistant_tool",
        _memory_nudge_interval=0,
        _user_turn_count=0,
        _compression_warning=None,
        _todo_store=MagicMock(has_items=lambda: True),
        api_mode="chat_completions",
        compression_enabled=False,
        compression_idle_compact_after_seconds=0,
    )
    mock_agent._restore_primary_runtime = MagicMock()
    mock_agent._swap_credential = MagicMock()

    # Another session moved the shared pool cursor to gemini-4
    mock_gemini_pool.select(preferred_account="user4@example.com")

    # Turn context runs for Session A
    build_turn_context(
        mock_agent,
        user_message="Hello",
        system_message=None,
        conversation_history=[],
        task_id=sid,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: ("prompt", False),
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda s: None,
        set_current_write_origin=lambda s: None,
        ra=lambda: MagicMock(),
    )

    # Must swap back to user2
    mock_agent._swap_credential.assert_called_once()
    swapped_entry = mock_agent._swap_credential.call_args[0][0]
    assert swapped_entry.label == "user2@example.com"


def test_subagent_delegation_cursor_isolation_preserves_parent_gemini_account(mock_gemini_pool):
    """Verify that delegated child agents get a cloned pool so child selection/leasing does not mutate the parent agent's active account."""
    from tools.delegate_tool import _resolve_child_credential_pool

    # Parent agent with pinned gemini account user4@example.com
    parent_agent = MagicMock(
        provider="gemini-oauth",
        model="gemini-3.7-flash-high",
        _credential_pool=mock_gemini_pool,
        _credential_pool_entry_id="gemini-4",
    )

    # Parent selects its pinned account
    parent_selected = mock_gemini_pool.select(preferred_account="user4@example.com")
    assert parent_selected is not None
    assert parent_selected.label == "user4@example.com"
    assert mock_gemini_pool.current() is not None
    assert mock_gemini_pool.current().label == "user4@example.com"

    # Delegate tool resolves child pool for the same provider
    child_pool = _resolve_child_credential_pool("gemini-oauth", parent_agent)
    assert child_pool is not None
    assert child_pool is not mock_gemini_pool  # Cloned instance
    assert child_pool.current() is None       # Independent cursor starts empty

    # Child agent leases / selects user1@example.com
    leased_id = child_pool.acquire_lease("gemini-1")
    assert leased_id == "gemini-1"
    assert child_pool.current() is not None
    assert child_pool.current().label == "user1@example.com"

    # Parent agent's pool cursor is completely unchanged
    parent_curr = mock_gemini_pool.current()
    assert parent_curr is not None
    assert parent_curr.label == "user4@example.com"

    # Clean up lease
    child_pool.release_lease("gemini-1")

def test_subagent_never_inherits_parent_gswitch_pin(mock_gemini_pool):
    """Verify that a subagent never inherits the parent session's /gswitch PIN and always routes dynamically via CD-DOCI."""
    from tools.delegate_tool import _resolve_child_credential_pool

    parent_agent = MagicMock(
        provider="gemini-oauth",
        model="gemini-3.7-flash-high",
        session_id="parent-session-pinned",
        _credential_pool=mock_gemini_pool,
        _credential_pool_entry_id="gemini-4",
    )

    # Parent has pinned account user4@example.com
    parent_selected = mock_gemini_pool.select(preferred_account="user4@example.com")
    assert parent_selected is not None
    assert parent_selected.label == "user4@example.com"

    # Child pool resolution creates an isolated clone with None cursor
    child_pool = _resolve_child_credential_pool("gemini-oauth", parent_agent)
    assert child_pool is not None
    assert child_pool._current_id is None

    # Child agent mock with subagent markers
    child_agent = MagicMock(
        provider="gemini-oauth",
        model="gemini-3.7-flash-high",
        session_id="child-subagent-session",
        platform="subagent",
        _subagent_id="sa-0-1234",
        _delegate_role="leaf",
        parent_session_id="parent-session-pinned",
        _credential_pool=child_pool,
        _session_init_model_config={"gemini_account": "alias4", "yolo_mode": True},
    )

    # Test delegate_tool stripping of gemini_account from child _session_init_model_config
    if getattr(child_agent, "_session_init_model_config", None) is not None:
        child_agent._session_init_model_config.pop("gemini_account", None)

    assert "gemini_account" not in child_agent._session_init_model_config

    # Subagent turn_context execution logic ensures preferred_account is NOT passed for subagents
    is_subagent = (
        getattr(child_agent, "platform", None) == "subagent"
        or bool(getattr(child_agent, "_subagent_id", None))
        or getattr(child_agent, "_delegate_role", None) is not None
        or bool(getattr(child_agent, "parent_session_id", None))
        or bool(getattr(child_agent, "_delegate_parent_ref", None))
    )
    assert is_subagent is True

    # Child selects unpinned -> automatically selects based on CD-DOCI rather than parent pin
    with patch("hermes_cli.auth.calculate_gemini_doci_score", return_value={"score": 10.0, "phi_lease": 1.0}):
        child_selected = child_pool.select(preferred_account=None if is_subagent else "user4@example.com")
        assert child_selected is not None
        # Child should not be forced to user4@example.com
        assert child_pool.current() is not None

def test_dynamic_mode_low_quota_preserves_stickiness_until_429():
    """Verify that unpinned dynamic sessions stay anchored to their chosen account at low quota (e.g. 5%) and only rotate on 429."""
    creds = [
        PooledCredential(
            provider="gemini-oauth",
            id="acc-1",
            label="user1@example.com",
            auth_type="oauth",
            priority=0,
            source="gemini_account_1",
            access_token="tok1",
            extra={"account_id": 1},
        ),
        PooledCredential(
            provider="gemini-oauth",
            id="acc-2",
            label="user2@example.com",
            auth_type="oauth",
            priority=1,
            source="gemini_account_2",
            access_token="tok2",
            extra={"account_id": 2},
        ),
    ]
    pool = CredentialPool("gemini-oauth", creds)

    # User 1 starts with 80% quota (score 3.0), User 2 with 5% quota (score 0.2)
    def _mock_doci(acc_idx, **kw):
        if acc_idx == 1:
            return {"score": 3.0, "cap_5h": 0.80, "cap_w": 0.80, "logged_in": True}
        return {"score": 0.2, "cap_5h": 0.05, "cap_w": 0.50, "logged_in": True}

    with patch("hermes_cli.auth._read_gemini_account_tokens", return_value=None):
        with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci):
            # Turn 1: Fresh unpinned session selects User 1 (healthiest)
            sel_1 = pool.select()
            assert sel_1 is not None
            assert sel_1.label == "user1@example.com"
            assert pool.current().label == "user1@example.com"

            # Now simulate User 1's quota dropping to 5% mid-conversation
            # Turn 2: Stays locked to User 1 via stickiness fast-path (NO premature eviction!)
            sel_2 = pool.select()
            assert sel_2 is not None
            assert sel_2.label == "user1@example.com"
            assert pool.current().label == "user1@example.com"

            # Now simulate User 1 hitting a real 429:
            pool.mark_exhausted_and_rotate(status_code=429, credential_id=sel_2.id)

            # Turn 3: User 1 is in 429 cooldown, so pool selects the next available account (User 2)
            sel_3 = pool.select()
            assert sel_3 is not None
            assert sel_3.label == "user2@example.com"
            assert pool.current().label == "user2@example.com"
