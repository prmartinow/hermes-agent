"""Tests for Concurrency-Dampened Dynamic Opportunity-Cost Index (CD-DOCI) rotation and subagent allocation.

Covers:
1. Mathematical CD-DOCI scoring with active lease dampener (Phi(L) = exp(-0.40 * L)).
2. Subagent allocation direct binding to leased credential (eliminating index 0 / default fallback).
3. Power-of-Two Choices (d=2) load dispersal across candidate accounts during concurrent bursts.
4. KV-cache session stickiness (session stays pinned to bound account).
5. Active session lease acquisition and release lifecycle.
"""

import unittest
from unittest.mock import MagicMock, patch
import math

from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli.auth import calculate_gemini_doci_score, _normalize_gemini_account_id


class TestCDDOCIScoring(unittest.TestCase):
    """Verify CD-DOCI mathematical scoring and concurrency dampening."""

    def test_active_lease_dampening(self):
        """Active leases apply exponential dampener exp(-0.40 * L)."""
        mock_status = {
            "logged_in": True,
            "email": "user@google.com",
            "quota": {
                "gemini_5h_percent": 80.0,
                "gemini_weekly_percent": 90.0,
                "gemini_5h_reset": "2h 30m",
                "gemini_weekly_reset": "3d 12h",
            },
        }

        with patch("hermes_cli.auth.get_gemini_oauth_auth_status", return_value=mock_status):
            score_0 = calculate_gemini_doci_score(1, active_leases=0)
            score_1 = calculate_gemini_doci_score(1, active_leases=1)
            score_2 = calculate_gemini_doci_score(1, active_leases=2)

            self.assertAlmostEqual(score_0["phi_lease"], 1.0, places=3)
            self.assertAlmostEqual(score_1["phi_lease"], math.exp(-0.40), places=3)
            self.assertAlmostEqual(score_2["phi_lease"], math.exp(-0.80), places=3)

            self.assertGreater(score_0["score"], score_1["score"])
            self.assertGreater(score_1["score"], score_2["score"])
            self.assertAlmostEqual(score_1["score"], score_0["score"] * math.exp(-0.40), places=3)

    def test_continuous_capacity_and_hard_gate_at_zero(self):
        """Low capacity (e.g. 15%) remains positive; strictly 0.0% is hard-gated to 0.0."""
        mock_status_low = {
            "logged_in": True,
            "email": "user@google.com",
            "quota": {
                "gemini_5h_percent": 15.0,
                "gemini_weekly_percent": 80.0,
            },
        }
        mock_status_zero = {
            "logged_in": True,
            "email": "user@google.com",
            "quota": {
                "gemini_5h_percent": 0.0,
                "gemini_weekly_percent": 80.0,
            },
        }

        with patch("hermes_cli.auth.get_gemini_oauth_auth_status", return_value=mock_status_low):
            score_low = calculate_gemini_doci_score(1)
            self.assertGreater(score_low["score"], 0.0)

        with patch("hermes_cli.auth.get_gemini_oauth_auth_status", return_value=mock_status_zero):
            score_zero = calculate_gemini_doci_score(1)
            self.assertEqual(score_zero["score"], 0.0)


class TestSubagentAllocation(unittest.TestCase):
    """Verify subagents bind to leased credentials directly."""

    def test_subagent_lease_acquisition_sets_current_and_retrieves_entry(self):
        """acquire_lease() must set _current_id and get_entry() must return the leased entry."""
        creds = [
            PooledCredential(
                provider="gemini-oauth",
                id="acc-1",
                label="gemini-1",
                auth_type="oauth",
                priority=0,
                source="gemini_account_1",
                access_token="tok-1",
                extra={"account_id": 1},
            ),
            PooledCredential(
                provider="gemini-oauth",
                id="acc-2",
                label="gemini-2",
                auth_type="oauth",
                priority=1,
                source="gemini_account_2",
                access_token="tok-2",
                extra={"account_id": 2},
            ),
        ]

        parent_pool = CredentialPool("gemini-oauth", creds)
        child_pool = parent_pool.clone()

        # Simulate lease acquisition for subagent
        leased_id = child_pool.acquire_lease("acc-2")
        self.assertEqual(leased_id, "acc-2")
        self.assertEqual(child_pool._current_id, "acc-2")

        # get_entry directly returns the target credential
        assert leased_id is not None
        leased_entry = child_pool.get_entry(leased_id)
        self.assertIsNotNone(leased_entry)
        assert leased_entry is not None
        self.assertEqual(leased_entry.id, "acc-2")

        # Parent cursor is untouched
        self.assertIsNone(parent_pool._current_id)

    def test_run_single_child_swaps_to_leased_entry(self):
        """tools/delegate_tool._run_single_child must swap child to leased entry."""
        from tools.delegate_tool import _run_single_child

        leased_entry = MagicMock()
        leased_entry.id = "acc-3"

        child = MagicMock()
        child._credential_pool = MagicMock()
        child._credential_pool.acquire_lease.return_value = "acc-3"
        child._credential_pool.get_entry.return_value = leased_entry
        child._credential_pool.current.return_value = leased_entry
        child.run_conversation.return_value = {
            "final_response": "done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        parent = MagicMock()
        parent.session_id = "parent-123"

        result = _run_single_child(
            task_index=0,
            goal="test goal",
            child=child,
            parent_agent=parent,
        )

        self.assertEqual(result["status"], "completed")
        child._swap_credential.assert_called_once_with(leased_entry)
        child._credential_pool.release_lease.assert_called_once_with("acc-3")


class TestPowerOfTwoChoices(unittest.TestCase):
    """Verify Power-of-Two Choices (d=2) selection under CD-DOCI."""

    def test_power_of_two_selection_picks_higher_doci_candidate(self):
        """d=2 draws candidates and selects the higher CD-DOCI score."""
        creds = [
            PooledCredential(
                provider="gemini-oauth",
                id="acc-1",
                label="gemini-1",
                auth_type="oauth",
                priority=0,
                source="gemini_account_1",
                access_token="tok-1",
                extra={"account_id": 1},
            ),
            PooledCredential(
                provider="gemini-oauth",
                id="acc-2",
                label="gemini-2",
                auth_type="oauth",
                priority=1,
                source="gemini_account_2",
                access_token="tok-2",
                extra={"account_id": 2},
            ),
            PooledCredential(
                provider="gemini-oauth",
                id="acc-3",
                label="gemini-3",
                auth_type="oauth",
                priority=2,
                source="gemini_account_3",
                access_token="tok-3",
                extra={"account_id": 3},
            ),
        ]

        pool = CredentialPool("gemini-oauth", creds)

        # Mock DOCI scores: acc 2 has highest score
        def mock_doci(account_id, model_group="gemini", active_leases=0):
            scores = {1: 0.20, 2: 0.95, 3: 0.40}
            base = scores.get(account_id, 0.1)
            return {"score": base * math.exp(-0.40 * active_leases)}

        with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=mock_doci), \
             patch("random.sample", return_value=[creds[0], creds[1]]):  # sampled acc 1 and acc 2
            chosen, _ = pool._select_unlocked(preferred_account=None)
            self.assertIsNotNone(chosen)
            assert chosen is not None
            self.assertEqual(chosen.id, "acc-2")

    def test_session_stickiness_preserves_bound_account(self):
        """Preferred account (pinned session) always sticks regardless of pool state."""
        creds = [
            PooledCredential(
                provider="gemini-oauth",
                id="acc-1",
                label="gemini-1",
                auth_type="oauth",
                priority=0,
                source="gemini_account_1",
                access_token="tok-1",
                extra={"account_id": 1},
            ),
            PooledCredential(
                provider="gemini-oauth",
                id="acc-2",
                label="gemini-2",
                auth_type="oauth",
                priority=1,
                source="gemini_account_2",
                access_token="tok-2",
                extra={"account_id": 2},
            ),
        ]

        pool = CredentialPool("gemini-oauth", creds)

        chosen, _ = pool._select_unlocked(preferred_account="1")
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.id, "acc-1")

        chosen2, _ = pool._select_unlocked(preferred_account="2")
        self.assertIsNotNone(chosen2)
        assert chosen2 is not None
        self.assertEqual(chosen2.id, "acc-2")


if __name__ == "__main__":
    unittest.main()
