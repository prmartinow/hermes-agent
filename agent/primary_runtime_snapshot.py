"""The ``_primary_runtime`` snapshot: the route a fallback turn restores on the next turn.

Both builders (agent construction, and a deliberate model switch) record the custom entry that owns
the route's credential, resolved against the config the route runs on. Restoring scopes the
credential pool by that record, never by a live ``config.yaml`` lookup, so an entry edited or
removed mid-session can't hand the session another account's key.
"""

from __future__ import annotations

import contextlib
import copy
from typing import Any

from agent.custom_entry_pool import SNAPSHOT_OWNER_KEY, runtime_entry_owner
from agent.route_verdicts import ROUTE_VERDICT_FIELDS


def _snapshot_primary_runtime(agent):
    # Per-turn restoration snapshot: after a fallback, the next turn restores these so the
    # preferred model gets a fresh attempt.
    _cc = agent.context_compressor
    agent._primary_runtime = {
        "model": agent.model,
        "provider": agent.provider,
        "requested_provider": agent.requested_provider,
        SNAPSHOT_OWNER_KEY: runtime_entry_owner(agent.provider, agent.requested_provider, agent.base_url),
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": getattr(agent, "api_key", ""),
        "request_overrides": dict(getattr(agent, "request_overrides", {}) or {}),
        "client_kwargs": dict(agent._client_kwargs),
        "use_prompt_caching": agent._use_prompt_caching,
        "use_native_cache_layout": agent._use_native_cache_layout,
        "reasoning_echo_flag": getattr(agent, "_reasoning_echo_flag", False),
        # Engine state _try_activate_fallback() overwrites (getattr: plugin engines may lack them).
        "compressor_model": getattr(_cc, "model", agent.model),
        "compressor_base_url": getattr(_cc, "base_url", agent.base_url),
        "compressor_api_key": getattr(_cc, "api_key", ""),
        "compressor_provider": getattr(_cc, "provider", agent.provider),
        "compressor_context_length": _cc.context_length,
        "compressor_threshold_tokens": _cc.threshold_tokens,
    }
    if agent.api_mode == "anthropic_messages":
        agent._primary_runtime.update({
            "anthropic_api_key": agent._anthropic_api_key,
            "anthropic_base_url": agent._anthropic_base_url,
            "is_anthropic_oauth": agent._is_anthropic_oauth,
        })


def _build_primary_runtime_snapshot(agent, api_mode) -> dict[str, Any]:
    """The ``_primary_runtime`` record that persists a switch across turns."""
    cc = getattr(agent, "context_compressor", None) or None
    rt = {
        "model": agent.model,
        "provider": agent.provider,
        "requested_provider": agent.requested_provider,
        SNAPSHOT_OWNER_KEY: runtime_entry_owner(agent.provider, agent.requested_provider, agent.base_url),
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": getattr(agent, "api_key", ""),
        "client_kwargs": dict(agent._client_kwargs),
        "use_prompt_caching": agent._use_prompt_caching,
        "use_native_cache_layout": agent._use_native_cache_layout,
        "reasoning_config": dict(agent.reasoning_config) if getattr(agent, "reasoning_config", None) else None,
        "reasoning_echo_flag": getattr(agent, "_reasoning_echo_flag", False),
        # Overrides must travel with the switched-to identity or a later recovery/restore resurrects
        # PRE-switch overrides from the stale init snapshot.
        # See #75091.
        "request_overrides": dict(getattr(agent, "request_overrides", {}) or {}),
        "runtime_capabilities": dict(getattr(agent, "runtime_capabilities", {}) or {}),
        "compressor_model": getattr(cc, "model", agent.model),
        "compressor_base_url": getattr(cc, "base_url", agent.base_url),
        "compressor_api_key": getattr(cc, "api_key", ""),
        "compressor_provider": getattr(cc, "provider", agent.provider),
        "compressor_context_length": cc.context_length if cc else 0,
        "compressor_api_mode": getattr(cc, "api_mode", agent.api_mode),
        "compressor_threshold_tokens": cc.threshold_tokens if cc else 0,
    }
    if api_mode == "anthropic_messages":
        rt.update({
            "anthropic_api_key": agent._anthropic_api_key,
            "anthropic_base_url": agent._anthropic_base_url,
            "is_anthropic_oauth": agent._is_anthropic_oauth,
        })
    return rt


# Pool reload is part of the switch and must be reversible on rollback, hence the pool fields.
_SWITCH_SNAPSHOT_FIELDS = (
    "model", "provider", "requested_provider", "base_url", "api_mode", "api_key", "client",
    "_anthropic_client", "_anthropic_api_key", "_anthropic_base_url", "_is_anthropic_oauth",
    "_config_context_length", "_reasoning_echo_flag", "runtime_capabilities",
    "_credential_pool", "_credential_pool_entry_id", *ROUTE_VERDICT_FIELDS,
    "_usage_anchor", "_turn_base_usage_anchor",
)
_MISSING = object()


def _snapshot_switch_state(agent) -> dict[str, Any]:
    """Snapshot every field the swap+rebuild mutates so a failed rebuild rolls back atomically
    (else a new model name + OLD client 400s next turn). The sentinel distinguishes unset from
    None: tests build bare agents via ``__new__`` without all fields."""
    snapshot = {name: getattr(agent, name, _MISSING) for name in _SWITCH_SNAPSHOT_FIELDS}
    # Shallow-copy the dict so mutating the live one doesn't poison the rollback target.
    snapshot["_client_kwargs"] = dict(getattr(agent, "_client_kwargs", {}) or {})
    if isinstance(snapshot.get("_usage_anchor"), dict):
        snapshot["_usage_anchor"] = copy.deepcopy(snapshot["_usage_anchor"])
    if isinstance(snapshot.get("_turn_base_usage_anchor"), dict):
        snapshot["_turn_base_usage_anchor"] = copy.deepcopy(snapshot["_turn_base_usage_anchor"])
    return snapshot


def _restore_switch_snapshot(agent, snapshot: dict[str, Any]) -> None:
    for name, value in snapshot.items():
        if value is _MISSING:
            continue  # attribute did not exist before the swap; don't fabricate it
        with contextlib.suppress(Exception):
            setattr(agent, name, value)
    if "_usage_anchor" in snapshot and snapshot["_usage_anchor"] is not _MISSING:
        with contextlib.suppress(Exception):
            from agent.usage_anchor import persist_usage_anchor
            persist_usage_anchor(agent, snapshot["_usage_anchor"])
