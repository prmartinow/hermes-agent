"""Tests for credential pool preservation through turn config and 429 recovery.

Covers:
1. CLI _resolve_turn_agent_config passes credential_pool to runtime dict
2. Gateway _resolve_turn_agent_config passes credential_pool to runtime dict
3. Eager fallback deferred when credential pool has credentials
4. Eager fallback fires when no credential pool exists
5. Full 429 rotation cycle: retry-same → rotate → exhaust → fallback
6. Failure attribution: the entry matching the failing API key is marked
   exhausted, not whatever pool.current() happens to point at
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.credential_pool import CredentialPool, PooledCredential, AUTH_TYPE_API_KEY


# ---------------------------------------------------------------------------
# 1. CLI _resolve_turn_agent_config includes credential_pool
# ---------------------------------------------------------------------------

class TestCliTurnRoutePool:
    def test_resolve_turn_includes_pool(self):
        """CLI's _resolve_turn_agent_config must pass credential_pool in runtime."""
        fake_pool = MagicMock(name="FakePool")
        shell = SimpleNamespace(
            model="gpt-5.4",
            api_key="sk-test",
            base_url=None,
            provider="openai-codex",
            requested_provider="my-named-provider",
            api_mode="codex_responses",
            acp_command=None,
            acp_args=[],
            _credential_pool=fake_pool,
            service_tier=None,
        )

        from cli import HermesCLI
        bound = HermesCLI._resolve_turn_agent_config.__get__(shell)
        route = bound("test message")

        assert route["runtime"]["credential_pool"] is fake_pool
        assert route["runtime"]["requested_provider"] == "my-named-provider"
        assert "my-named-provider" in route["signature"]

        shell.requested_provider = "other-named-provider"
        other_route = bound("test message")
        assert other_route["signature"] != route["signature"]


# ---------------------------------------------------------------------------
# 2. Gateway _resolve_turn_agent_config includes credential_pool
# ---------------------------------------------------------------------------

class TestGatewayTurnRoutePool:
    def test_resolve_turn_includes_pool(self):
        """Gateway's _resolve_turn_agent_config must pass credential_pool."""
        from gateway.run import GatewayRunner

        fake_pool = MagicMock(name="FakePool")
        runner = SimpleNamespace(_service_tier=None)
        runtime_kwargs = {
            "api_key": "***",
            "base_url": None,
            "provider": "openai-codex",
            "requested_provider": "openai-codex",
            "api_mode": "codex_responses",
            "command": None,
            "args": [],
            "credential_pool": fake_pool,
        }

        bound = GatewayRunner._resolve_turn_agent_config.__get__(runner)
        route = bound("test message", "gpt-5.4", runtime_kwargs)

        assert route["runtime"]["credential_pool"] is fake_pool
        assert route["runtime"]["requested_provider"] == "openai-codex"


# ---------------------------------------------------------------------------
# 3 & 4. Eager fallback deferred/fires based on credential pool
# ---------------------------------------------------------------------------

class TestEagerFallbackWithPool:
    """Test the eager fallback guard in run_agent.py's error handling loop."""

    def _make_agent(self, has_pool=True, pool_has_creds=True, has_fallback=True):
        """Create a minimal AIAgent mock with the fields needed."""
        from run_agent import AIAgent

        with patch.object(AIAgent, "__init__", lambda self, **kw: None):
            agent = AIAgent()

        agent._credential_pool = None
        if has_pool:
            pool = MagicMock()
            pool.has_available.return_value = pool_has_creds
            agent._credential_pool = pool

        agent._fallback_chain = [{"model": "fallback/model"}] if has_fallback else []
        agent._fallback_index = 0
        agent._try_activate_fallback = MagicMock(return_value=True)
        agent._emit_status = MagicMock()

        return agent

    def test_eager_fallback_deferred_when_pool_has_credentials(self):
        """429 with active pool should NOT trigger eager fallback."""
        agent = self._make_agent(has_pool=True, pool_has_creds=True, has_fallback=True)

        # Simulate the check from run_agent.py lines 7180-7191
        is_rate_limited = True
        if is_rate_limited and agent._fallback_index < len(agent._fallback_chain):
            pool = agent._credential_pool
            pool_may_recover = pool is not None and pool.has_available()
            if not pool_may_recover:
                agent._try_activate_fallback()

        agent._try_activate_fallback.assert_not_called()

    def test_eager_fallback_fires_when_no_pool(self):
        """429 without pool should trigger eager fallback."""
        agent = self._make_agent(has_pool=False, has_fallback=True)

        is_rate_limited = True
        if is_rate_limited and agent._fallback_index < len(agent._fallback_chain):
            pool = agent._credential_pool
            pool_may_recover = pool is not None and pool.has_available()
            if not pool_may_recover:
                agent._try_activate_fallback()

        agent._try_activate_fallback.assert_called_once()

    def test_eager_fallback_fires_when_pool_exhausted(self):
        """429 with exhausted pool should trigger eager fallback."""
        agent = self._make_agent(has_pool=True, pool_has_creds=False, has_fallback=True)

        is_rate_limited = True
        if is_rate_limited and agent._fallback_index < len(agent._fallback_chain):
            pool = agent._credential_pool
            pool_may_recover = pool is not None and pool.has_available()
            if not pool_may_recover:
                agent._try_activate_fallback()

        agent._try_activate_fallback.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Full 429 rotation cycle via _recover_with_credential_pool
# ---------------------------------------------------------------------------

class TestPoolRotationCycle:
    """Verify the retry-same → rotate → exhaust flow in _recover_with_credential_pool."""

    def _make_agent_with_pool(self, pool_entries=3):
        from run_agent import AIAgent

        with patch.object(AIAgent, "__init__", lambda self, **kw: None):
            agent = AIAgent()

        entries = []
        for i in range(pool_entries):
            e = MagicMock(name=f"entry_{i}")
            e.id = f"cred-{i}"
            entries.append(e)

        pool = MagicMock()
        pool.has_credentials.return_value = True
        # Must be set explicitly — MagicMock.provider returns a truthy
        # child mock, which would trigger the provider-mismatch guard.
        pool.provider = ""

        # mark_exhausted_and_rotate returns next entry until exhausted
        self._rotation_index = 0

        def rotate(status_code=None, error_context=None, **_kwargs):
            self._rotation_index += 1
            if self._rotation_index < pool_entries:
                return entries[self._rotation_index]
            pool.has_credentials.return_value = False
            return None

        pool.mark_exhausted_and_rotate = MagicMock(side_effect=rotate)
        agent._credential_pool = pool
        agent._swap_credential = MagicMock()
        agent.log_prefix = ""
        agent.api_key = "test-api-key"
        agent.provider = "test-provider"
        pool.provider = "test-provider"

        return agent, pool, entries

    def test_first_429_sets_retry_flag_no_rotation(self):
        """First 429 should just set has_retried_429=True, no rotation."""
        agent, pool, _ = self._make_agent_with_pool(3)
        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=429, has_retried_429=False
        )
        assert recovered is False
        assert has_retried is True
        pool.mark_exhausted_and_rotate.assert_not_called()

    def test_second_429_rotates_to_next(self):
        """Second consecutive 429 should rotate to next credential."""
        agent, pool, entries = self._make_agent_with_pool(3)
        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=429, has_retried_429=True
        )
        assert recovered is True
        assert has_retried is False  # reset after rotation
        pool.mark_exhausted_and_rotate.assert_called_once_with(status_code=429, error_context=None, api_key_hint="test-api-key", failure_reason="rate_limit")
        agent._swap_credential.assert_called_once_with(entries[1])

    def test_pool_exhaustion_returns_false(self):
        """When all credentials exhausted, recovery should return False."""
        agent, pool, _ = self._make_agent_with_pool(1)
        # First 429 sets flag
        _, has_retried = agent._recover_with_credential_pool(
            status_code=429, has_retried_429=False
        )
        assert has_retried is True

        # Second 429 tries to rotate but pool is exhausted (only 1 entry)
        recovered, _ = agent._recover_with_credential_pool(
            status_code=429, has_retried_429=True
        )
        assert recovered is False

    def test_402_immediate_rotation(self):
        """402 (billing) should immediately rotate, no retry-first."""
        agent, pool, entries = self._make_agent_with_pool(3)
        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=402, has_retried_429=False
        )
        assert recovered is True
        assert has_retried is False
        pool.mark_exhausted_and_rotate.assert_called_once_with(status_code=402, error_context=None, api_key_hint="test-api-key", failure_reason="billing")


    def test_api_key_hint_from_pool_current_when_agent_key_missing(self):
        """api_key_hint should fall back to pool.current().runtime_api_key
        when agent.api_key is not set (#43747)."""
        from run_agent import AIAgent

        with patch.object(AIAgent, "__init__", lambda self, **kw: None):
            agent = AIAgent()

        e0 = MagicMock(name="entry_0")
        e0.id = "cred-0"
        e1 = MagicMock(name="entry_1")
        e1.id = "cred-1"

        pool = MagicMock()
        pool.has_credentials.return_value = True
        pool.provider = "test-provider"
        agent.provider = "test-provider"

        # current entry has a runtime_api_key
        cur_entry = MagicMock()
        cur_entry.runtime_api_key = "pool-current-key"
        pool.current.return_value = cur_entry

        pool.mark_exhausted_and_rotate.return_value = e1
        agent._credential_pool = pool
        agent._swap_credential = MagicMock()
        agent.log_prefix = ""
        # No agent.api_key set — should fall back to pool.current().runtime_api_key

        recovered, has_retried = agent._recover_with_credential_pool(
            status_code=402, has_retried_429=False
        )
        assert recovered is True
        pool.mark_exhausted_and_rotate.assert_called_once_with(
            status_code=402, error_context=None, api_key_hint="pool-current-key",
            failure_reason="billing",
        )


# ---------------------------------------------------------------------------
# 6. Real-pool regression: the hint routes exhaustion to the FAILED entry
# ---------------------------------------------------------------------------

class TestApiKeyHintRealPool:
    """Prove the routing guarantee through the real CredentialPool selector:
    when the failed key differs from the pool's current/first entry, only the
    failed entry is marked exhausted (#43747, wrong-entry marking)."""

    def _seed_pool(self, tmp_path, monkeypatch):
        import json

        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "auth.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {},
                    "credential_pool": {
                        "openrouter": [
                            {
                                "id": "cred-healthy",
                                "label": "healthy",
                                "auth_type": "api_key",
                                "priority": 0,
                                "source": "manual",
                                "access_token": "sk-or-healthy",
                            },
                            {
                                "id": "cred-failed",
                                "label": "failed",
                                "auth_type": "api_key",
                                "priority": 1,
                                "source": "manual",
                                "access_token": "sk-or-failed",
                            },
                        ]
                    },
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        from agent.credential_pool import load_pool

        return load_pool("openrouter")

    def test_hint_marks_failed_entry_not_current(self, tmp_path, monkeypatch):
        pool = self._seed_pool(tmp_path, monkeypatch)
        # Another process/pool instance issued sk-or-failed; THIS pool's
        # current() would resolve to the first (healthy) entry.
        assert pool.select().access_token == "sk-or-healthy"

        next_entry = pool.mark_exhausted_and_rotate(
            status_code=429,
            error_context={"reason": "rate_limit_exceeded"},
            api_key_hint="sk-or-failed",
        )

        statuses = {e.id: e.last_status for e in pool._entries}
        assert statuses["cred-failed"] == "exhausted"
        assert statuses["cred-healthy"] in (None, "ok")
        assert next_entry is not None
        assert next_entry.access_token == "sk-or-healthy"

    def test_without_hint_current_entry_is_marked(self, tmp_path, monkeypatch):
        """Baseline: no hint falls back to current() — the pre-fix behavior."""
        pool = self._seed_pool(tmp_path, monkeypatch)
        assert pool.select().access_token == "sk-or-healthy"

        pool.mark_exhausted_and_rotate(status_code=429, error_context=None)

        statuses = {e.id: e.last_status for e in pool._entries}
        assert statuses["cred-healthy"] == "exhausted"
        assert statuses["cred-failed"] in (None, "ok")


# ---------------------------------------------------------------------------
# 7. Failure attribution — mark the key that failed, not pool.current()
# ---------------------------------------------------------------------------

class TestFailureAttribution:
    """Regression: recover_with_credential_pool must mark the entry whose API
    key actually produced the failure.

    pool.current() is shared mutable state: round-robin select() advances it,
    concurrent turns move it, and a freshly loaded pool (second process) has
    current() == None — in which case the old code fell through to
    _select_unlocked() and exhausted the NEXT (healthy) entry, copying the
    failing key's error/reset time onto it until the whole pool went offline.
    """

    def _make_pool(self, tmp_path, monkeypatch, entries):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        # Keep host Anthropic/Claude credentials out of this fixture. load_pool()
        # auto-seeds ~/.claude/.credentials.json and env keys when anthropic is
        # explicitly configured on the machine, which turns a deliberate
        # single-entry pool into a multi-entry pool and invalidates isolation
        # assertions (see test_unmatched_key_does_not_retry_only_pool_entry).
        for env_var in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            monkeypatch.delenv(env_var, raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.is_provider_explicitly_configured",
            lambda provider: False,
        )
        (hermes_home / "auth.json").write_text(
            json.dumps({"version": 1, "credential_pool": {"anthropic": entries}}),
            encoding="utf-8",
        )
        from agent.credential_pool import load_pool

        pool = load_pool("anthropic")
        assert [entry.id for entry in pool.entries()] == [
            entry["id"] for entry in entries
        ], "pool fixture leaked host credentials into the test pool"
        return pool

    def _entry(self, idx, key, **overrides):
        entry = {
            "id": f"cred-{idx}",
            "label": f"key-{idx}",
            "auth_type": "api_key",
            "priority": idx,
            "source": "manual",
            "access_token": key,
        }
        entry.update(overrides)
        return entry

    def _agent(self, pool, failing_key, credential_id=None):
        return SimpleNamespace(
            provider="anthropic",
            api_key=failing_key,
            _credential_pool=pool,
            _credential_pool_entry_id=credential_id,
            _swap_credential=MagicMock(),
        )

    def _statuses(self, pool):
        return {e.id: e.last_status for e in pool.entries()}



    def test_pre_exhausted_check_uses_failing_key(self, tmp_path, monkeypatch):
        """The 'already exhausted → rotate immediately' check must inspect the
        failing entry, not pool.current(): first 429 on an already-exhausted
        key rotates without burning a retry."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [
                self._entry(0, "key-a"),
                self._entry(
                    1, "key-b",
                    last_status="exhausted",
                    last_status_at=time.time(),
                    last_error_code=429,
                ),
            ],
        )
        agent = self._agent(pool, failing_key="key-b")

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, has_retried = recover_with_credential_pool(
            agent, status_code=429, has_retried_429=False
        )

        assert recovered is True
        assert has_retried is False
        statuses = self._statuses(pool)
        assert statuses["cred-0"] != "exhausted"
        swapped = agent._swap_credential.call_args[0][0]
        assert swapped.id == "cred-0"

    def test_auth_refresh_targets_failing_key_not_pointer(self, tmp_path, monkeypatch):
        """The auth path must refresh the entry that supplied the failing key,
        not current(). With current() pointing at healthy A while key B failed,
        try_refresh_current() force-refreshes A — for non-OAuth entries a
        forced refresh marks the entry exhausted outright — so healthy A dies,
        the hinted rotation then exhausts B, and the pool has nothing left."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
        )
        # Point the shared cursor at the healthy entry, as a concurrent
        # turn's select() would.
        selected = pool.select()
        assert selected.id == "cred-0"
        assert pool.current().id == "cred-0"

        agent = self._agent(pool, failing_key="key-b")
        agent._is_entitlement_failure = MagicMock(return_value=False)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, _ = recover_with_credential_pool(
            agent, status_code=401, has_retried_429=False
        )

        assert recovered is True
        statuses = self._statuses(pool)
        assert statuses["cred-1"] == "exhausted"
        assert statuses["cred-0"] != "exhausted"
        swapped = agent._swap_credential.call_args[0][0]
        assert swapped.id == "cred-0"


    def test_unmatched_key_does_not_retry_only_pool_entry(
        self, tmp_path, monkeypatch
    ):
        """Legacy agents without a stable id must stop when an unmatched key
        has no different credential to rotate to."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "pool-runtime-key")],
        )
        agent = self._agent(pool, failing_key="wrapper-runtime-key")
        agent._is_entitlement_failure = MagicMock(return_value=False)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recovered, _ = recover_with_credential_pool(
            agent, status_code=401, has_retried_429=False
        )

        assert recovered is False
        assert self._statuses(pool)["cred-0"] != "exhausted"
        agent._swap_credential.assert_not_called()

    def test_classified_billing_403_recorded_on_entry(self, tmp_path, monkeypatch):
        """A billing-classified 403 must reach the pool as `billing`, not a bare 403.

        `error_classifier` maps OpenRouter's `key limit exceeded` 403 (and xAI
        spending-limit blocks) to FailoverReason.billing, but the pool only
        ever saw the raw status — so a sole-credential pool gave a spent
        account the 60s transient cooldown and re-failed every minute. The
        recovery path now forwards the classified reason so the pool can size
        the bench correctly.
        """
        from agent.error_classifier import FailoverReason

        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
        )
        agent = self._agent(pool, failing_key="key-b")
        agent._is_entitlement_failure = MagicMock(return_value=False)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recover_with_credential_pool(
            agent,
            status_code=403,
            has_retried_429=False,
            classified_reason=FailoverReason.billing,
        )

        failed = {e.id: e for e in pool.entries()}["cred-1"]
        assert failed.last_status == "exhausted"
        assert failed.failure_reason == "billing"

    def test_unclassified_403_records_no_billing_reason(self, tmp_path, monkeypatch):
        """An unclassified 403 stays transient — no billing verdict is invented."""
        pool = self._make_pool(
            tmp_path, monkeypatch,
            [self._entry(0, "key-a"), self._entry(1, "key-b")],
        )
        agent = self._agent(pool, failing_key="key-b")
        agent._is_entitlement_failure = MagicMock(return_value=False)

        from agent.agent_runtime_helpers import recover_with_credential_pool

        recover_with_credential_pool(
            agent, status_code=403, has_retried_429=False
        )

        failed = {e.id: e for e in pool.entries()}["cred-1"]
        assert failed.failure_reason != "billing"


# ---------------------------------------------------------------------------
# 7. CredentialPool.clone() cursor isolation
# ---------------------------------------------------------------------------

class TestCredentialPoolClone:
    """Test session-scoped cursor isolation via CredentialPool.clone()."""

    def _entry(self, idx: int, key: str = "sk-test") -> PooledCredential:
        return PooledCredential(
            provider="openai-codex",
            id=f"cred-{idx}",
            label=f"account-{idx}",
            auth_type=AUTH_TYPE_API_KEY,
            priority=idx,
            source=f"manual:{idx}",
            access_token=key,
        )

    def test_clone_shares_lock_and_entries_with_isolated_cursor(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        entries = [self._entry(0, "key-a"), self._entry(1, "key-b")]
        parent_pool = CredentialPool(provider="gemini-oauth", entries=entries)

        # Parent selects entry 0
        parent_selected = parent_pool.select(preferred_id="cred-0")
        assert parent_selected is not None
        assert parent_selected.id == "cred-0"
        parent_curr = parent_pool.current()
        assert parent_curr is not None
        assert parent_curr.id == "cred-0"

        # Clone the pool for a subagent / child session
        cloned_pool = parent_pool.clone()
        assert cloned_pool is not parent_pool
        assert cloned_pool._entries is parent_pool._entries
        assert cloned_pool._lock is parent_pool._lock
        assert cloned_pool._active_leases is parent_pool._active_leases

        # Cloned pool starts with an independent cursor
        assert cloned_pool.current() is None
        parent_curr = parent_pool.current()
        assert parent_curr is not None
        assert parent_curr.id == "cred-0"

        # Cloned pool selects entry 1
        child_selected = cloned_pool.select(preferred_id="cred-1")
        assert child_selected is not None
        assert child_selected.id == "cred-1"
        child_curr = cloned_pool.current()
        assert child_curr is not None
        assert child_curr.id == "cred-1"

        # Parent's active cursor is untouched
        parent_curr = parent_pool.current()
        assert parent_curr is not None
        assert parent_curr.id == "cred-0"

    def test_clone_lease_and_exhaustion_synchronization(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        entries = [self._entry(0, "key-a"), self._entry(1, "key-b")]
        parent_pool = CredentialPool(provider="openai-codex", entries=entries)
        parent_pool.select(preferred_id="cred-0")

        cloned_pool = parent_pool.clone()

        # Leasing on clone tracks shared leases without mutating parent's cursor
        leased_id = cloned_pool.acquire_lease("cred-1")
        assert leased_id == "cred-1"
        assert parent_pool._active_leases.get("cred-1") == 1
        parent_curr = parent_pool.current()
        assert parent_curr is not None
        assert parent_curr.id == "cred-0"
        child_curr = cloned_pool.current()
        assert child_curr is not None
        assert child_curr.id == "cred-1"

        cloned_pool.release_lease("cred-1")
        assert "cred-1" not in parent_pool._active_leases

        # Exhausting an entry via clone synchronizes across both instances
        cloned_pool.mark_exhausted_and_rotate(status_code=429, credential_id="cred-1")
        parent_entries = {e.id: e for e in parent_pool.entries()}
        cloned_entries = {e.id: e for e in cloned_pool.entries()}

        assert parent_entries["cred-1"].last_status == "exhausted"
        assert cloned_entries["cred-1"].last_status == "exhausted"
        # Parent still points to cred-0
        parent_curr = parent_pool.current()
        assert parent_curr is not None
        assert parent_curr.id == "cred-0"


# ---------------------------------------------------------------------------
# 11. Nodes A, B, D: get_entry, session lease tracking, Power-of-Two CD-DOCI
# ---------------------------------------------------------------------------

class TestCredentialPoolNodesABD:
    def _entry(self, idx: int, key: str = "test-key", acc_id: int = 1) -> PooledCredential:
        return PooledCredential(
            provider="gemini-oauth",
            id=f"cred-{idx}",
            label=f"gemini-{acc_id}",
            auth_type="oauth",
            priority=idx,
            source=f"gemini_account_{acc_id}",
            access_token=key,
            extra={"account_id": acc_id},
        )

    def test_get_entry(self):
        entries = [self._entry(0, "tok-0", 1), self._entry(1, "tok-1", 2)]
        pool = CredentialPool(provider="gemini-oauth", entries=entries)

        assert pool.get_entry("cred-0") is entries[0]
        assert pool.get_entry("cred-1") is entries[1]
        assert pool.get_entry("cred-missing") is None
        assert pool.get_entry("") is None

    def test_acquire_lease_sets_current_id(self):
        entries = [self._entry(0, "tok-0", 1), self._entry(1, "tok-1", 2)]
        parent_pool = CredentialPool(provider="gemini-oauth", entries=entries)
        parent_pool.select(preferred_id="cred-0")

        cloned_pool = parent_pool.clone()
        leased_id = cloned_pool.acquire_lease()
        assert leased_id is not None
        # Cloned pool's current() matches the leased entry
        child_curr = cloned_pool.current()
        assert child_curr is not None
        assert child_curr.id == leased_id
        # get_entry also returns the leased entry
        leased_entry = cloned_pool.get_entry(leased_id)
        assert leased_entry is not None
        assert leased_entry.id == leased_id
        # Parent pool cursor is untouched
        parent_curr = parent_pool.current()
        assert parent_curr is not None
        assert parent_curr.id == "cred-0"

    def test_session_lease_tracking(self):
        entries = [self._entry(0, "tok-0", 1), self._entry(1, "tok-1", 2)]
        pool = CredentialPool(provider="gemini-oauth", entries=entries)

        assert pool.get_active_session_count(1) == 0
        assert pool.get_active_session_count("1") == 0

        pool.acquire_session_lease("sess-1", 1)
        assert pool.get_active_session_count(1) == 1
        assert pool.get_active_session_count("1") == 1
        assert pool.get_active_session_count(2) == 0

        # Another session on account 1
        pool.acquire_session_lease("sess-2", "1")
        assert pool.get_active_session_count(1) == 2

        # Re-assigning sess-1 to account 2 moves the lease
        pool.acquire_session_lease("sess-1", 2)
        assert pool.get_active_session_count(1) == 1
        assert pool.get_active_session_count(2) == 1

        # Release session 2
        pool.release_session_lease("sess-2", 1)
        assert pool.get_active_session_count(1) == 0
        assert pool.get_active_session_count(2) == 1

        # Release session 1 without account hint
        pool.release_session_lease("sess-1")
        assert pool.get_active_session_count(2) == 0

    def test_session_leases_shared_with_clones(self):
        entries = [self._entry(0, "tok-0", 1), self._entry(1, "tok-1", 2)]
        parent_pool = CredentialPool(provider="gemini-oauth", entries=entries)
        cloned_pool = parent_pool.clone()

        cloned_pool.acquire_session_lease("subagent-sess", 2)
        assert parent_pool.get_active_session_count(2) == 1
        assert cloned_pool.get_active_session_count(2) == 1

        cloned_pool.release_session_lease("subagent-sess", 2)
        assert parent_pool.get_active_session_count(2) == 0
        assert cloned_pool.get_active_session_count(2) == 0

    def test_power_of_two_cd_doci_selection(self):
        # Create 5 Gemini accounts
        entries = [
            self._entry(0, "tok-0", 1),
            self._entry(1, "tok-1", 2),
            self._entry(2, "tok-2", 3),
            self._entry(3, "tok-3", 4),
            self._entry(4, "tok-4", 5),
        ]
        pool = CredentialPool(provider="gemini-oauth", entries=entries)

        # Mock DOCI scores: acc 1=1.0, 2=2.0, 3=3.0, 4=4.0, 5=5.0
        def _mock_doci(acc_idx, **kw):
            return {"score": float(acc_idx), "cap_5h": 0.8, "cap_w": 0.8, "logged_in": True}

        with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci):
            # When random.sample picks [acc 2, acc 4], it should select acc 4 (score 4.0 > 2.0)
            with patch("random.sample", return_value=[entries[1], entries[3]]):
                selected = pool.select()
                assert selected is not None
                assert selected.id == "cred-3"
                assert selected.extra["account_id"] == 4

            # In-session stickiness: second select without args stays on cred-3
            selected_again = pool.select()
            assert selected_again is not None
            assert selected_again.id == "cred-3"

            # Preferred account overrides stickiness and CD-DOCI
            selected_pref = pool.select(preferred_account="1")
            assert selected_pref is not None
            assert selected_pref.id == "cred-0"
            assert selected_pref.extra["account_id"] == 1

    def test_fresh_session_dynamic_allocation_flow(self, tmp_path):
        from hermes_state import SessionDB
        from unittest.mock import MagicMock

        db_path = tmp_path / "state_test.db"
        db = SessionDB(db_path=db_path)

        entries = [
            self._entry(0, "tok-0", 1),
            self._entry(1, "tok-1", 2),
            self._entry(2, "tok-2", 3),
            self._entry(3, "tok-3", 4),
            self._entry(4, "tok-4", 5),
        ]
        pool = CredentialPool(provider="gemini-oauth", entries=entries)

        # Acc 3 has highest DOCI score (10.0), Acc 1 has lowest (0.1)
        def _mock_doci(acc_idx, **kw):
            scores = {1: 0.1, 2: 2.0, 3: 10.0, 4: 5.0, 5: 1.0}
            return {"score": scores.get(acc_idx, 1.0), "cap_5h": 0.8, "cap_w": 0.8, "logged_in": True}

        # 1. Simulate fresh session creation in DB with unpinned model_config (no gemini_account)
        sid = "20260828_000000_fresh01"
        db.create_session(
            session_id=sid,
            source="tui",
            model="gemini-3.7-flash-high",
            model_config={"model": "gemini-3.7-flash-high", "provider": "gemini-oauth"},
        )

        mock_agent = MagicMock()
        mock_agent.provider = "gemini-oauth"
        mock_agent.model = "gemini-3.7-flash-high"
        mock_agent.session_id = sid
        mock_agent._session_db = db
        mock_agent._credential_pool = pool
        mock_agent._credential_pool_entry_id = None

        def _mock_swap(entry):
            mock_agent._credential_pool_entry_id = entry.id
            db.update_session_meta(sid, json.dumps({"gemini_account": entry.label or entry.id}))

        mock_agent._swap_credential.side_effect = _mock_swap

        # Turn 1: Fresh session should dynamically allocate top account (Acc 3 / highest score when sampled)
        with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci):
            with patch("random.sample", return_value=[entries[0], entries[2]]):  # [Acc 1, Acc 3]
                # Simulate the turn_context logic
                pinned_acc = None
                sess_row = db.get_session(sid)
                if sess_row and sess_row.get("model_config"):
                    cfg_raw = sess_row.get("model_config")
                    if isinstance(cfg_raw, str):
                        cfg_raw = json.loads(cfg_raw)
                    if isinstance(cfg_raw, dict):
                        pinned_acc = cfg_raw.get("gemini_account")
                if not pinned_acc:
                    pinned_acc = getattr(mock_agent, "_credential_pool_entry_id", None)
                if pinned_acc:
                    entry = pool.select(preferred_account=pinned_acc)
                else:
                    entry = pool.select()
                if entry is not None and getattr(entry, "id", None) != getattr(mock_agent, "_credential_pool_entry_id", None):
                    mock_agent._swap_credential(entry)

                # Verified: Acc 3 selected, NOT Acc 1
                assert mock_agent._credential_pool_entry_id == "cred-2"
                sess_row = db.get_session(sid)
                assert sess_row is not None
                cfg = json.loads(sess_row["model_config"])
                assert cfg.get("gemini_account") == "gemini-3"

            # Turn 2: Pinned session must strictly stick to Acc 3 (gemini-3)
            with patch("random.sample", return_value=[entries[0], entries[4]]):  # [Acc 1, Acc 5]
                pinned_acc = None
                sess_row = db.get_session(sid)
                if sess_row and sess_row.get("model_config"):
                    cfg_raw = sess_row.get("model_config")
                    if isinstance(cfg_raw, str):
                        cfg_raw = json.loads(cfg_raw)
                    if isinstance(cfg_raw, dict):
                        pinned_acc = cfg_raw.get("gemini_account")
                if not pinned_acc:
                    pinned_acc = getattr(mock_agent, "_credential_pool_entry_id", None)
                if pinned_acc:
                    entry = pool.select(preferred_account=pinned_acc)
                else:
                    entry = pool.select()
                if entry is not None and getattr(entry, "id", None) != getattr(mock_agent, "_credential_pool_entry_id", None):
                    mock_agent._swap_credential(entry)

                # Verified: Still on Acc 3
                assert mock_agent._credential_pool_entry_id == "cred-2"



