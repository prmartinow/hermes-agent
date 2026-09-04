"""Tests for tools/context_retrieve_tool.py."""

import sqlite3
import uuid
import pytest

from tools.context_retrieve_tool import (
    CONTEXT_RETRIEVE_SCHEMA,
    check_context_retrieve_requirements,
    context_retrieve,
    _retrieve_from_db_direct,
)
from tools.registry import registry


class TestContextRetrieveTool:
    def test_schema_registered(self):
        assert "context_retrieve" in registry.get_all_tool_names()
        entry = registry.get_entry("context_retrieve")
        assert entry is not None
        schema = entry.schema
        assert schema is not None
        assert schema["name"] == "context_retrieve"
        assert "archive_id" in schema["parameters"]["properties"]
        assert "offset" in schema["parameters"]["properties"]
        assert "limit" in schema["parameters"]["properties"]

    def test_check_requirements(self):
        assert check_context_retrieve_requirements() is True

    def test_missing_archive_id(self):
        result = context_retrieve("")
        assert "error" in result.lower() or "required" in result.lower()

    def test_nonexistent_archive_id(self):
        fake_id = str(uuid.uuid4())
        result = context_retrieve(fake_id)
        assert f"Archive ID '{fake_id}' not found" in result

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
            raw_text = "\n".join(lines)
            test_id = "test-uuid-999"
            conn.execute(
                "INSERT INTO content_archives VALUES (?, ?, ?, ?, ?, ?)",
                (test_id, "test-session", "terminal", raw_text, len(raw_text), "2026-08-26T00:00:00Z")
            )
        conn.close()

        import tools.context_retrieve_tool as crt
        monkeypatch.setattr(crt, "DB_PATH", str(test_db))
        # Force fallback to direct db query to test against test_db
        monkeypatch.setattr(crt, "_get_retrieve_content_fn", lambda: None)

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
