"""Targeted regression tests for bounded history backend fix.

Covers:
1. Subagent flooding: >100 subagents hiding user sessions, and SQL filtering BEFORE pagination
2. Paging totals: offset, limit, total, has_more
3. Non-Gemini sessions: cross-provider sessions included by default (scope="all") vs scope="gemini"
4. Switched models: per-turn attribution across model switches without overwriting historical turns
5. Multi-model turns: turns with multiple models marked as 'mixed' with distinct models represented
6. Legacy ambiguity: legacy turns without model metadata remain model=None/provenance="unknown"
   (rejecting scout fallback to current session model or cumulative usage ranges)
7. Assistant message metadata persistence: model & provider merged into display_metadata
"""

import json
from unittest.mock import MagicMock
import pytest
from hermes_state import SessionDB
from hermes_cli.auth import list_gemini_session_histories
from hermes_cli.web_routers.gemini import get_gemini_session_histories
from agent.session_persistence import _db_flush_row
from agent.chat_completion_helpers import build_assistant_message


@pytest.fixture
def mock_hermes_env(tmp_path, monkeypatch):
    """Set up temporary state.db and config."""
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
    return db, tmp_path


def test_subagent_flooding_sql_filtering_and_paging(mock_hermes_env):
    """Test reproduction and fix of subagent flooding:
    Seed 1 older user session and 115 newer subagent sessions.
    With include_subagents=False and limit=50, the user session MUST be returned on page 1,
    and total must reflect 1 user session (not 116 total sessions).
    """
    db, _ = mock_hermes_env

    # 1. Create 1 user session (older timestamp)
    user_sid = "20260901_000000_user1"
    db.create_session(
        session_id=user_sid,
        source="cli",
        model="gemini-3.7-flash",
        model_config={"model": "gemini-3.7-flash", "provider": "gemini-oauth"},
        system_prompt="",
    )
    db.append_messages_batch(
        user_sid,
        [
            {"role": "user", "content": "User prompt in main chat", "timestamp": 1757000000.0},
            {
                "role": "assistant",
                "content": "Assistant answer",
                "timestamp": 1757000001.0,
                "display_metadata": {"model": "gemini-3.7-flash", "provider": "gemini-oauth"},
            },
        ],
    )

    # 2. Create 115 subagent sessions (newer timestamps)
    for i in range(115):
        sub_sid = f"20260901_010000_subagent_{i:03d}"
        db.create_session(
            session_id=sub_sid,
            source="subagent",
            model="gemini-3.7-flash",
            parent_session_id=user_sid,
            model_config={"model": "gemini-3.7-flash", "provider": "gemini-oauth"},
            system_prompt="",
        )
        db.append_messages_batch(
            sub_sid,
            [
                {"role": "user", "content": f"Subagent prompt {i}", "timestamp": 1757000100.0 + i},
                {
                    "role": "assistant",
                    "content": f"Subagent answer {i}",
                    "timestamp": 1757000101.0 + i,
                    "display_metadata": {"model": "gemini-3.7-flash", "provider": "gemini-oauth"},
                },
            ],
        )

    # REPRODUCTION VERIFICATION:
    # When include_subagents=True and limit=100, the top 100 are subagents, masking the user session
    res_all = list_gemini_session_histories(limit=100, offset=0, include_subagents=True)
    assert res_all["total"] == 116
    assert len(res_all["sessions"]) == 100
    user_sess_in_all = [s for s in res_all["sessions"] if s["session_id"] == user_sid]
    assert len(user_sess_in_all) == 0  # Masked! User session flooded out

    # FIX VERIFICATION:
    # With include_subagents=False, SQL filters BEFORE LIMIT so user session is immediately returned
    res_user = list_gemini_session_histories(limit=50, offset=0, include_subagents=False)
    assert res_user["total"] == 1
    assert len(res_user["sessions"]) == 1
    assert res_user["has_more"] is False
    assert res_user["sessions"][0]["session_id"] == user_sid
    assert res_user["sessions"][0]["is_subagent"] is False

    # Test via router wrapper as well
    router_res = get_gemini_session_histories(limit=50, offset=0, include_subagents=False)
    assert router_res["total"] == 1
    assert len(router_res["sessions"]) == 1
    assert router_res["sessions"][0]["session_id"] == user_sid


def test_pagination_totals_and_has_more(mock_hermes_env):
    """Test pagination bounds, offsets, totals, and has_more flag."""
    db, _ = mock_hermes_env

    # Create 5 distinct user sessions
    for i in range(5):
        sid = f"20260901_00000{i}_page"
        db.create_session(
            session_id=sid,
            source="cli",
            model="gemini-3.7-flash",
            system_prompt="",
        )
        db.append_messages_batch(
            sid,
            [{"role": "user", "content": f"Page prompt {i}", "timestamp": 1757000000.0 + i}],
        )

    # Page 1: limit 2, offset 0 -> 2 items, total 5, has_more True
    p1 = list_gemini_session_histories(limit=2, offset=0, include_subagents=False)
    assert p1["total"] == 5
    assert len(p1["sessions"]) == 2
    assert p1["limit"] == 2
    assert p1["offset"] == 0
    assert p1["has_more"] is True

    # Page 2: limit 2, offset 2 -> 2 items, total 5, has_more True
    p2 = list_gemini_session_histories(limit=2, offset=2, include_subagents=False)
    assert p2["total"] == 5
    assert len(p2["sessions"]) == 2
    assert p2["offset"] == 2
    assert p2["has_more"] is True
    # Ensure items are distinct
    assert p1["sessions"][0]["session_id"] != p2["sessions"][0]["session_id"]

    # Page 3: limit 2, offset 4 -> 1 item, total 5, has_more False
    p3 = list_gemini_session_histories(limit=2, offset=4, include_subagents=False)
    assert p3["total"] == 5
    assert len(p3["sessions"]) == 1
    assert p3["offset"] == 4
    assert p3["has_more"] is False


def test_non_gemini_conversations_default_accessible(mock_hermes_env):
    """Test that conversations across providers (gpt-6-astra, claude-3-5-sonnet)
    are returned by default (scope='all') rather than filtered out.
    """
    db, _ = mock_hermes_env

    # 1. Create Gemini session
    db.create_session(session_id="sess_gemini", source="cli", model="gemini-3.7-flash", system_prompt="")
    db.append_messages_batch("sess_gemini", [{"role": "user", "content": "Gemini hi", "timestamp": 1757000010.0}])

    # 2. Create OpenAI / Codex session
    db.create_session(session_id="sess_codex", source="cli", model="gpt-6-astra", system_prompt="")
    db.append_messages_batch("sess_codex", [{"role": "user", "content": "Codex hi", "timestamp": 1757000020.0}])

    # 3. Create Anthropic Claude session
    db.create_session(session_id="sess_claude", source="cli", model="claude-3-5-sonnet", system_prompt="")
    db.append_messages_batch("sess_claude", [{"role": "user", "content": "Claude hi", "timestamp": 1757000030.0}])

    # Scope 'all' (default): all 3 returned
    res_all = list_gemini_session_histories(limit=50, scope="all")
    assert res_all["total"] == 3
    sids = [s["session_id"] for s in res_all["sessions"]]
    assert "sess_gemini" in sids
    assert "sess_codex" in sids
    assert "sess_claude" in sids

    # Scope 'gemini': only Gemini session returned
    res_gem = list_gemini_session_histories(limit=50, scope="gemini")
    assert res_gem["total"] == 1
    assert res_gem["sessions"][0]["session_id"] == "sess_gemini"


def test_switched_models_per_turn_attribution(mock_hermes_env):
    """Test that when a session switches models (e.g. Turn 1 on gemini-3.7-flash,
    then switched to gemini-3.8-flash-high), historical turns retain their exact model
    rather than being overwritten by the current session.model.
    """
    db, _ = mock_hermes_env

    sid = "20260901_modelswitch"
    db.create_session(
        session_id=sid,
        source="cli",
        model="gemini-3.8-flash-high",  # Overwritten current session model!
        system_prompt="",
    )

    # Turn 1 executed under gemini-3.7-flash
    db.append_messages_batch(
        sid,
        [
            {"role": "user", "content": "Turn 1 question", "timestamp": 1757000000.0},
            {
                "role": "assistant",
                "content": "Turn 1 answer",
                "timestamp": 1757000002.0,
                "display_metadata": {"model": "gemini-3.7-flash", "provider": "gemini-oauth"},
            },
        ],
    )

    # Turn 2 executed under gemini-3.8-flash-high
    db.append_messages_batch(
        sid,
        [
            {"role": "user", "content": "Turn 2 question", "timestamp": 1757000010.0},
            {
                "role": "assistant",
                "content": "Turn 2 answer",
                "timestamp": 1757000012.0,
                "display_metadata": {"model": "gemini-3.8-flash-high", "provider": "gemini-oauth"},
            },
        ],
    )

    res = list_gemini_session_histories(limit=10)
    sess = next(s for s in res["sessions"] if s["session_id"] == sid)
    assert sess["model"] == "gemini-3.8-flash-high"  # Current session model

    turns = [e for e in sess["events"] if e.get("event_type") == "turn"]
    assert len(turns) == 2

    # Turn 1 must NOT be overwritten to gemini-3.8-flash-high
    assert turns[0]["model"] == "gemini-3.7-flash"
    assert turns[0]["models"] == ["gemini-3.7-flash"]
    assert turns[0]["provider"] == "gemini-oauth"
    assert turns[0]["model_provenance"] == "message_metadata"

    # Turn 2 has gemini-3.8-flash-high
    assert turns[1]["model"] == "gemini-3.8-flash-high"
    assert turns[1]["models"] == ["gemini-3.8-flash-high"]
    assert turns[1]["provider"] == "gemini-oauth"
    assert turns[1]["model_provenance"] == "message_metadata"


def test_multi_model_in_single_turn_marks_mixed(mock_hermes_env):
    """Test that multiple models in one turn must not falsely stamp one:
    it represents distinct models and marks model='mixed'.
    """
    db, _ = mock_hermes_env

    sid = "20260901_mixedturn"
    db.create_session(session_id=sid, source="cli", model="gemini-3.7-flash", system_prompt="")

    db.append_messages_batch(
        sid,
        [
            {"role": "user", "content": "Mixed turn question", "timestamp": 1757000000.0},
            {
                "role": "assistant",
                "content": "Call tool",
                "timestamp": 1757000001.0,
                "tool_calls": [{"id": "c1", "function": {"name": "test_tool"}}],
                "display_metadata": {"model": "gemini-3.7-flash", "provider": "gemini-oauth"},
            },
            {
                "role": "tool",
                "content": "Tool result",
                "timestamp": 1757000002.0,
                "tool_name": "test_tool",
            },
            {
                "role": "assistant",
                "content": "Final answer after fallback model switch",
                "timestamp": 1757000003.0,
                "display_metadata": {"model": "gemini-2.5-pro", "provider": "gemini-oauth"},
            },
        ],
    )

    res = list_gemini_session_histories(limit=10)
    sess = next(s for s in res["sessions"] if s["session_id"] == sid)
    turns = [e for e in sess["events"] if e.get("event_type") == "turn"]
    assert len(turns) == 1
    t = turns[0]

    assert t["model"] == "mixed"
    assert t["models"] == ["gemini-3.7-flash", "gemini-2.5-pro"]
    assert t["provider"] == "gemini-oauth"
    assert t["model_provenance"] == "mixed"


def test_legacy_ambiguity_rejects_scout_fallback(mock_hermes_env):
    """Test CRITICAL rejection of scout fallback:
    For legacy messages without model in display_metadata, the turn model must be
    None/null and provenance='unknown', rather than guessing from sessions.model
    or overlapping session_model_usage ranges.
    """
    db, _ = mock_hermes_env

    sid = "20260901_legacymsg"
    db.create_session(
        session_id=sid,
        source="cli",
        model="gemini-3.8-flash-high",  # Current session model
        system_prompt="",
    )

    # Seed cumulative session_model_usage ranges that scout proposed guessing from
    import sqlite3
    with sqlite3.connect(str(mock_hermes_env[1] / "state.db")) as conn:
        conn.execute("""
            INSERT INTO session_model_usage (
                session_id, model, billing_provider, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?)
        """, (sid, "gemini-3.7-flash", "gemini-oauth", 1757000000.0, 1757000050.0))

    # Turn with legacy assistant message: no model in display_metadata
    db.append_messages_batch(
        sid,
        [
            {"role": "user", "content": "Legacy turn question", "timestamp": 1757000010.0},
            {
                "role": "assistant",
                "content": "Legacy turn answer",
                "timestamp": 1757000012.0,
                # Legacy metadata only has gemini_account or is empty
                "display_metadata": {"gemini_account": "user1@example.com"},
            },
        ],
    )

    res = list_gemini_session_histories(limit=10)
    sess = next(s for s in res["sessions"] if s["session_id"] == sid)
    turns = [e for e in sess["events"] if e.get("event_type") == "turn"]
    assert len(turns) == 1
    t = turns[0]

    # Must be None, not falsely stamped with "gemini-3.8-flash-high" or guessed from usage ranges
    assert t["model"] is None
    assert t["models"] == []
    assert t["model_provenance"] == "unknown"


def test_assistant_message_metadata_persistence_merge():
    """Test that _db_flush_row merges actual model and provider into display_metadata
    while preserving existing metadata keys, and build_assistant_message attaches them.
    """
    mock_agent = MagicMock()
    mock_agent.model = "gemini-3.7-flash"
    mock_agent.provider = "gemini-oauth"
    mock_agent._credential_pool = None
    mock_agent._credential_pool_entry_id = None
    mock_agent._extract_reasoning = MagicMock(return_value=None)
    mock_agent._strip_think_blocks = lambda s: s

    # 1. build_assistant_message stashes model/provider on message dict
    raw_sdk_msg = MagicMock(
        content="Testing answer",
        tool_calls=None,
        reasoning_content=None,
        reasoning_details=None,
        model_extra=None,
        anthropic_content_blocks=None,
        codex_reasoning_items=None,
        codex_message_items=None,
    )
    built = build_assistant_message(mock_agent, raw_sdk_msg, finish_reason="stop")
    assert built.get("model") == "gemini-3.7-flash"
    assert built.get("provider") == "gemini-oauth"

    # 2. _db_flush_row merges model and provider into display_metadata at persistence time
    # With pre-existing custom key in display_metadata
    built["display_metadata"] = {"notification_category": "custom"}
    row = _db_flush_row(mock_agent, built, is_current_turn_user=False)
    meta = row["display_metadata"]
    assert isinstance(meta, dict)
    assert meta["model"] == "gemini-3.7-flash"
    assert meta["provider"] == "gemini-oauth"
    assert meta["notification_category"] == "custom"
