"""Tests for tools/context_retrieve_tool.py."""

import sqlite3
import uuid

from tools.context_retrieve_tool import (
    CONTEXT_RETRIEVE_SCHEMA,
    check_context_retrieve_requirements,
    context_retrieve,
)
from tools.registry import registry


class TestContextRetrieveTool:
    def test_schema_registered(self):
        from toolsets import resolve_toolset

        for toolset in ("file", "context"):
            assert "context_retrieve" in resolve_toolset(toolset)
        entry = registry.get_entry("context_retrieve")
        assert entry is not None
        schema = entry.schema
        assert schema is not None
        assert schema["name"] == "context_retrieve"
        assert "archive_id" in schema["parameters"]["properties"]
        assert "offset" in schema["parameters"]["properties"]
        assert "limit" in schema["parameters"]["properties"]

    def test_check_requirements(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_MEMORY_TRACES_PATH", str(tmp_path))
        assert check_context_retrieve_requirements() is False
        (tmp_path / "context.db").touch()
        assert check_context_retrieve_requirements() is True

    def test_missing_archive_id(self):
        result = context_retrieve("")
        assert "error" in result.lower() or "required" in result.lower()

    def test_nonexistent_archive_id(self):
        fake_id = str(uuid.uuid4())
        result = context_retrieve(fake_id)
        assert f"Archive ID '{fake_id}' not found" in result

    def test_retrieval_uses_the_active_profile(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_MEMORY_TRACES_PATH", raising=False)
        monkeypatch.delenv("AGENT_MEMORY_CONTEXT_DB_PATH", raising=False)
        homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
        for home, content in zip(homes, ("first profile", "second profile")):
            traces = home / "agent-memory" / "traces"
            traces.mkdir(parents=True)
            with sqlite3.connect(traces / "context.db") as conn:
                conn.execute("CREATE TABLE content_archives (archive_id TEXT, tool_name TEXT, raw_content TEXT)")
                conn.execute("INSERT INTO content_archives VALUES (?, ?, ?)", ("same-id", "terminal", content))

        monkeypatch.setenv("HERMES_HOME", str(homes[0]))
        assert context_retrieve("same-id") == "first profile"
        monkeypatch.setenv("HERMES_HOME", str(homes[1]))
        assert context_retrieve("same-id") == "second profile"

    def test_multiplexed_profile_does_not_inherit_launch_profile_paths(self, tmp_path, monkeypatch):
        from agent import secret_scope
        from tools.context_retrieve_tool import _db_path

        home = tmp_path / "profile-b"
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("AGENT_MEMORY_TRACES_PATH", str(tmp_path / "launch-traces"))
        monkeypatch.setenv("AGENT_MEMORY_CONTEXT_DB_PATH", str(tmp_path / "launch-context.db"))
        secret_scope.set_multiplex_active(True)
        try:
            token = secret_scope.set_secret_scope({}, profile_home=str(home))
            try:
                assert _db_path() == home / "agent-memory" / "traces" / "context.db"
            finally:
                secret_scope.reset_secret_scope(token)
            assert check_context_retrieve_requirements() is False
            assert "not found" in context_retrieve("same-id").lower()
        finally:
            secret_scope.set_multiplex_active(False)

    def test_full_retrieval_and_slicing(self, tmp_path, monkeypatch):
        # Create a test sqlite db
        test_db = tmp_path / "test_context.db"
        conn = sqlite3.connect(test_db)
        with conn:
            conn.execute("""
                CREATE TABLE content_archives (
                    archive_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    raw_content TEXT NOT NULL,
                    content_size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            lines = [f"output_line_{i}" for i in range(1, 11)]
            raw_text = "\r\n".join(lines) + "\r\n"
            test_id = "test-uuid-999"
            conn.execute(
                "INSERT INTO content_archives VALUES (?, ?, ?, ?, ?, ?)",
                (test_id, "test-session", "terminal", raw_text, len(raw_text), "2026-08-26T00:00:00Z")
            )
        conn.close()

        monkeypatch.setenv("AGENT_MEMORY_CONTEXT_DB_PATH", str(test_db))

        # 1. Full retrieval
        full = context_retrieve(test_id)
        assert full == raw_text

        # 2. Sliced with offset and limit (1-indexed lines 3..5)
        sliced = context_retrieve(test_id, offset=3, limit=3)
        assert "Lines 3 to 5 of 10 total lines" in sliced
        assert "output_line_3\noutput_line_4\noutput_line_5" in sliced
        assert "output_line_1" not in sliced
        assert "output_line_6" not in sliced

        # 3. Offset beyond total lines
        out_of_bounds = context_retrieve(test_id, offset=20)
        assert "exceeds total lines" in out_of_bounds
