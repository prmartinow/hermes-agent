"""Tests for /gs slash command to switch active Gemini accounts dynamically.

Covers:
1. Dynamic account label resolution from config.yaml (display.account_aliases).
2. /gs <label> swaps live credentials and updates runtime cursor without DB model_config mutation.
3. Numerical arguments (/gs 2) are rejected with label instructions.
4. Unknown labels (/gs xyz) are rejected with available label list.
5. Bare /gs displays usage syntax.
6. Fallback reads last message display_metadata["gemini_account"] on cold restart.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from hermes_cli.auth import (
    get_gemini_account_label_map,
    handle_gs_command,
    resolve_session_last_used_account,
)


class TestGSCommand(unittest.TestCase):
    """Test /gs command behavior and contracts."""

    def test_dynamic_label_map_resolution(self):
        """Account labels are dynamically resolved from config.yaml display.account_aliases."""
        mock_cfg = {
            "display": {
                "account_aliases": {
                    "user1@example.com": "alias1",
                    "user2@example.com": "alias2",
                    "user3@example.com": "alias3",
                    "user4@example.com": "alias4",
                    "user5@example.com": "alias5",
                }
            }
        }

        def mock_status(idx):
            emails = {
                1: "user1@example.com",
                2: "user2@example.com",
                3: "user3@example.com",
                4: "user4@example.com",
                5: "user5@example.com",
            }
            return {"logged_in": True, "email": emails.get(idx, "")}

        with patch("hermes_cli.config.load_config", return_value=mock_cfg), \
             patch("hermes_cli.auth.get_gemini_oauth_auth_status", side_effect=mock_status):
            label_map = get_gemini_account_label_map()
            self.assertEqual(label_map.get("alias1"), 1)
            self.assertEqual(label_map.get("alias2"), 2)
            self.assertEqual(label_map.get("alias3"), 3)
            self.assertEqual(label_map.get("alias4"), 4)
            self.assertEqual(label_map.get("alias5"), 5)

    def test_gs_success_swaps_credentials_and_updates_cursor(self):
        """/gs alias2 updates pool cursor and calls agent._swap_credential without mutating DB model_config."""
        agent = MagicMock()
        mock_pool = MagicMock()
        mock_entry = MagicMock(id="gemini-2", label="alias2")
        mock_pool.select.return_value = mock_entry
        agent._credential_pool = mock_pool

        def mock_status(idx):
            return {"logged_in": True, "email": "user2@example.com"}

        with patch("hermes_cli.auth.get_gemini_account_label_map", return_value={"alias2": 2, "alias1": 1}), \
             patch("hermes_cli.auth.get_gemini_oauth_auth_status", side_effect=mock_status):
            output = handle_gs_command(session_id="sess-123", arg="alias2", agent=agent)

            self.assertEqual(output, "✓ Switched to alias2")
            agent._swap_credential.assert_called_once_with(mock_entry)
            self.assertEqual(mock_pool._current_id, "gemini-2")
            self.assertEqual(agent._credential_pool_entry_id, "gemini-2")

    def test_gs_rejects_numeric_arguments(self):
        """/gs 2 is rejected with a message requiring account labels."""
        with patch("hermes_cli.auth.get_gemini_account_label_map", return_value={"alias1": 1, "alias2": 2}):
            output = handle_gs_command("sess-123", "2")
            self.assertIn("Invalid account", output)
            self.assertIn("alias1", output)

    def test_gs_rejects_unknown_labels(self):
        """/gs xyz is rejected with available label guidance."""
        with patch("hermes_cli.auth.get_gemini_account_label_map", return_value={"alias1": 1, "alias2": 2}):
            output = handle_gs_command("sess-123", "xyz")
            self.assertIn("Unknown account 'xyz'", output)
            self.assertIn("alias1", output)

    def test_gs_bare_shows_usage(self):
        """Bare /gs displays usage syntax."""
        with patch("hermes_cli.auth.get_gemini_account_label_map", return_value={"alias1": 1}):
            output = handle_gs_command("sess-123", "")
            self.assertIn("Usage: /gs <label>", output)

    def test_cold_start_fallback_reads_last_message_metadata(self):
        """On cold start (empty in-memory cache), resolve_session_last_used_account reads last message metadata."""
        from hermes_cli.auth import _RESOLVED_SESSION_ACCOUNTS, _RESOLVED_SESSION_ACCOUNTS_LOCK

        # Clear in-memory cache for session
        with _RESOLVED_SESSION_ACCOUNTS_LOCK:
            _RESOLVED_SESSION_ACCOUNTS.pop("cold-session-1", None)

        mock_db = MagicMock()
        mock_db.get_session.return_value = {"model_config": "{}"}
        # Last message has display_metadata with gemini_account
        mock_db.get_messages.return_value = [
            {"id": "msg-1", "role": "user", "display_metadata": None},
            {"id": "msg-2", "role": "assistant", "display_metadata": json.dumps({"gemini_account": "alias3"})},
        ]

        resolved = resolve_session_last_used_account("cold-session-1", db=mock_db)
        self.assertEqual(resolved, "alias3")


if __name__ == "__main__":
    unittest.main()
