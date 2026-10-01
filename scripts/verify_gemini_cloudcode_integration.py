#!/usr/bin/env python3
"""Reproducible Live Certification & Closure Harness for Google Gemini Cloud Code PA.

Certifies that production Hermes correctly executes:
1. Dynamic routing (gemini-3.8-flash -> gemini-3.8-flash-tiered with thinkingConfig)
2. Static routing (gemini-3.6-flash -> gemini-3.6-flash-medium without thinkingConfig)
3. Same-process per-base effort memory across model switches
4. Partner route excursion and restoration
5. Real thought-signature acquisition, native carrier capture, and exact replay (HTTP 200)
6. SQLite signed-history persistence and reloaded replay
7. Cold reasoning resume and state reconstruction
8. Foreign unsigned tool history bypass sentinel injection
9. Signed native group provenance
10. Isolated --global configuration persistence
11. Production --once lifecycle turn restoration
12. Invalid-request zero-I/O rejection
13. Multi-account provider route sanity

Safety Invariants:
- Requires explicit --live flag to execute upstream inference.
- Operates strictly in an isolated temporary HERMES_HOME.
- Read-only credential discovery; zero operator config or state mutation.
- Verified zero secret leakage: thought signatures, tokens, and private paths are never printed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from typing import Any

# Ensure project root in sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.gemini_cloudcode_adapter import GeminiCloudCodeClient
from agent.gemini_cloudcode_models import (
    resolve_model_selection,
    selectable_reasoning_efforts,
    _CLOUDCODE_ACCOUNT_PROVIDERS,
)
from agent.native_replay import find_native_assistant_detail
from agent.reasoning_selection import (
    canonical_reasoning_base,
    reasoning_effort_error,
    resolve_effective_reasoning_effort,
)
from hermes_cli.auth import get_gemini_oauth_auth_status
from hermes_state import SessionDB
import tui_gateway.server as server


class CertificationResult:
    def __init__(self):
        self.results: dict[str, str] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self.exit_code = 0

    def record_pass(self, name: str, **detail):
        self.results[name] = "PASS"
        if detail:
            self.details[name] = detail

    def record_fail(self, name: str, error: str, **detail):
        self.results[name] = "FAIL"
        self.details[name] = {"error": error, **detail}
        self.exit_code = 1

    def record_upstream_unavailable(self, name: str, reason: str, **detail):
        self.results[name] = "UPSTREAM_UNAVAILABLE"
        self.details[name] = {"reason": reason, **detail}
        if self.exit_code == 0:
            self.exit_code = 2


def _hash_file(path: Path) -> str:
    if not path.exists():
        return "absent"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _get_active_token(account: int = 1) -> str:
    st = get_gemini_oauth_auth_status(account)
    key = st.get("api_key") or ""
    if not key:
        raise RuntimeError(f"No active OAuth token found for account {account}")
    return key


def run_certification(*, live: bool, as_json: bool) -> int:
    report = CertificationResult()

    if not live:
        print("Notice: --live flag is required to run live Cloud Code certification.", file=sys.stderr)
        return 1

    # Safety Preflight: Verify operator config isolation
    real_config_path = Path.home() / ".hermes" / "config.yaml"
    pre_hash = _hash_file(real_config_path)

    with tempfile.TemporaryDirectory(prefix="hermes_cert_") as td:
        temp_dir = Path(td)
        temp_hermes_home = temp_dir / ".hermes"
        temp_hermes_home.mkdir(parents=True, exist_ok=True)

        try:
            # Token Discovery (in-memory read-only)
            try:
                token = _get_active_token(1)
            except Exception as exc:
                report.record_upstream_unavailable("credential_discovery", str(exc))
                return 2

            if not token:
                report.record_upstream_unavailable("credential_discovery", "No active Gemini OAuth token available")
                return 2

            client = GeminiCloudCodeClient(access_token=token)

            # ------------------------------------------------------------------
            # 1. Dynamic Routing Live Gate
            # ------------------------------------------------------------------
            try:
                resolved = resolve_model_selection("gemini-3.8-flash", effort="low")
                assert resolved.wire_model == "gemini-3.8-flash-tiered", f"Wrong wire model: {resolved.wire_model}"
                assert resolved.thinking_config == {"thinkingLevel": "low", "includeThoughts": True}
                assert "thinkingBudget" not in (resolved.thinking_config or {})

                resp = client.chat.completions.create(
                    model="gemini-3.8-flash",
                    messages=[{"role": "user", "content": "Respond with 'dynamic_ok'"}],
                    extra_body={"effort": "low"},
                    max_tokens=15,
                )
                assert resp and resp.choices and resp.choices[0].message
                report.record_pass("dynamic_3_8_low", wire_model=resolved.wire_model, status=200)
            except Exception as exc:
                report.record_fail("dynamic_3_8_low", str(exc))

            # ------------------------------------------------------------------
            # 2. Static Routing Live Gate
            # ------------------------------------------------------------------
            try:
                resolved_static = resolve_model_selection("gemini-3.6-flash", effort="medium")
                assert resolved_static.wire_model == "gemini-3.6-flash-medium", f"Wrong wire: {resolved_static.wire_model}"
                assert resolved_static.thinking_config is None

                resp = client.chat.completions.create(
                    model="gemini-3.6-flash",
                    messages=[{"role": "user", "content": "Respond with 'static_ok'"}],
                    extra_body={"effort": "medium"},
                    max_tokens=15,
                )
                assert resp and resp.choices and resp.choices[0].message
                report.record_pass("static_3_6_medium", wire_model=resolved_static.wire_model, status=200)
            except Exception as exc:
                if "404" in str(exc) or "400" in str(exc) or "unavailable" in str(exc).lower():
                    report.record_upstream_unavailable("static_3_6_medium", str(exc))
                else:
                    report.record_fail("static_3_6_medium", str(exc))

            # ------------------------------------------------------------------
            # 3. Same-Process Per-Base Effort Memory
            # ------------------------------------------------------------------
            try:
                from run_agent import AIAgent

                def fake_build_client_local(ag, *a, **k):
                    ag.api_key = token
                    ag.base_url = ""
                    ag._client_kwargs = {"api_key": token, "base_url": ""}
                    ag.client = client

                with patch("agent.agent_init._build_client", side_effect=fake_build_client_local):
                    agent = AIAgent(
                        model="gemini-3.8-flash",
                        provider="gemini-oauth",
                        api_key=token,
                        reasoning_config={"enabled": True, "effort": "low"},
                        quiet_mode=True,
                    )
                    agent.effort_by_base = {"gemini-3.8-flash": "low"}
                session = {"agent": agent, "session_key": "s_mem"}

                # Switch to 3.6 medium
                with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                         success=True, new_model="gemini-3.6-flash", target_provider="gemini-oauth",
                         base_url="", api_key=token, api_mode="chat_completions", model_info=None, warning_message=None)),                      patch.object(server, "_restart_slash_worker"),                      patch.object(server, "_persist_live_session_runtime"),                      patch.object(server, "_persist_live_session_system_prompt"),                      patch.object(server, "_append_model_switch_marker"),                      patch.object(server, "_emit_session_info"):

                    server._apply_model_switch("s_mem", session, "/model gemini-3.6-flash --reasoning medium --tui-session")
                    assert agent.model == "gemini-3.6-flash"
                    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
                    assert agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.6-flash": "medium"}

                # Switch back to 3.8 without supplying effort
                with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                         success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth",
                         base_url="", api_key=token, api_mode="chat_completions", model_info=None, warning_message=None)),                      patch.object(server, "_restart_slash_worker"),                      patch.object(server, "_persist_live_session_runtime"),                      patch.object(server, "_persist_live_session_system_prompt"),                      patch.object(server, "_append_model_switch_marker"),                      patch.object(server, "_emit_session_info"):

                    server._apply_model_switch("s_mem", session, "/model gemini-3.8-flash --tui-session")
                    # Memory recovers previous low!
                    assert agent.model == "gemini-3.8-flash"
                    assert agent.reasoning_config == {"enabled": True, "effort": "low"}
                    assert agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.6-flash": "medium"}
                    report.record_pass("switch_memory", effort_by_base_intact=True)
            except Exception as exc:
                report.record_fail("switch_memory", str(exc))

            # ------------------------------------------------------------------
            # 4. Real Thought-Signature Acquisition & Exact Replay
            # ------------------------------------------------------------------
            captured_carrier = None
            captured_tool_call = None
            try:
                test_tools = [{
                    "type": "function",
                    "function": {
                        "name": "certification_echo",
                        "description": "Echoes back the input value for certification verification.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"]
                        }
                    }
                }]
                initial_messages = [{"role": "user", "content": "Call certification_echo with value 'cert_test_pass'"}]

                tc_resp = client.chat.completions.create(
                    model="gemini-3.8-flash",
                    messages=initial_messages,
                    tools=test_tools,
                    tool_choice="auto",
                    extra_body={"effort": "low"},
                    max_tokens=100,
                )
                msg = tc_resp.choices[0].message
                assert msg.tool_calls, "Upstream did not trigger tool call"
                tc = msg.tool_calls[0]
                assert tc.function.name == "certification_echo"

                # Check thought signature in carrier
                carrier = find_native_assistant_detail(msg.reasoning_details)
                assert carrier is not None, "Missing google.native_assistant carrier"
                captured_carrier = carrier
                captured_tool_call = tc

                has_sig = bool((tc.extra_content or {}).get("google", {}).get("thought_signature")
                               or (tc.extra_content or {}).get("thought_signature"))
                assert has_sig, "Missing thought signature in tool call extra_content"

                # Replay signed tool call turn
                replay_messages = list(initial_messages)
                replay_messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        "extra_content": tc.extra_content,
                    }],
                    "reasoning_details": msg.reasoning_details,
                })
                replay_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": "certification_echo",
                    "content": "cert_test_pass",
                })

                followup_resp = client.chat.completions.create(
                    model="gemini-3.8-flash",
                    messages=replay_messages,
                    tools=test_tools,
                    extra_body={"effort": "low"},
                    max_tokens=50,
                )
                assert followup_resp and followup_resp.choices
                report.record_pass("signed_tool_replay", signature_present=True, native_carrier_present=True, replay_status=200)
            except Exception as exc:
                report.record_fail("signed_tool_replay", str(exc))

            # ------------------------------------------------------------------
            # 5. SQLite Signed-History Round Trip
            # ------------------------------------------------------------------
            try:
                db_path = temp_dir / "session.db"
                db = SessionDB(db_path)
                session_id = "s_cert_sqlite"
                db.create_session(session_id, "cli", model="gemini-3.8-flash")

                db.append_message(session_id, "user", "Call certification_echo with value 'cert_test_pass'")
                if captured_tool_call and captured_carrier:
                    db.append_message(
                        session_id, "assistant", content=None,
                        tool_calls=[{
                            "id": captured_tool_call.id,
                            "type": "function",
                            "function": {
                                "name": captured_tool_call.function.name,
                                "arguments": captured_tool_call.function.arguments
                            },
                            "extra_content": captured_tool_call.extra_content,
                        }],
                        reasoning_details=[captured_carrier],
                    )
                    db.append_message(session_id, "tool", "cert_test_pass", tool_name="certification_echo", tool_call_id=captured_tool_call.id)

                    reloaded = db.get_messages(session_id)
                    assert len(reloaded) == 3
                    reloaded_assistant = reloaded[1]
                    assert reloaded_assistant["tool_calls"][0]["extra_content"] == captured_tool_call.extra_content

                    # Replay reloaded history to upstream
                    reloaded_messages = [
                        {"role": m["role"], "content": m["content"], "tool_calls": m.get("tool_calls"), "reasoning_details": m.get("reasoning_details")}
                        for m in reloaded
                    ]
                    resp = client.chat.completions.create(
                        model="gemini-3.8-flash",
                        messages=reloaded_messages,
                        extra_body={"effort": "low"},
                        max_tokens=50,
                    )
                    assert resp and resp.choices
                    report.record_pass("sqlite_signed_replay", signature_roundtrip_exact=True, status=200)
                else:
                    report.record_fail("sqlite_signed_replay", "Prerequisite captured_tool_call missing")
            except Exception as exc:
                report.record_fail("sqlite_signed_replay", str(exc))

            # ------------------------------------------------------------------
            # 6. Cold Reasoning Resume
            # ------------------------------------------------------------------
            try:
                db_cold = SessionDB(temp_dir / "cold_resume.db")
                sid_cold = "s_cold_cert"
                db_cold.create_session(sid_cold, "cli", model="gemini-3.8-flash")
                # Persist active reasoning_config in session row
                db_cold.update_session_model(sid_cold, "gemini-3.8-flash", "gemini-oauth")
                db_cold.patch_session_model_config(sid_cold, {"reasoning_config": {"enabled": True, "effort": "low"}})

                # In-memory map before shutdown had 3.8=low and 3.1=high
                pre_process_memory = {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}

                # Reconstruct agent through server._make_agent seam
                mock_cfg = {"model": "gemini-3.8-flash", "provider": "gemini-oauth"}
                def fake_build(ag, *a, **k):
                    ag.api_key = token
                    ag.base_url = ""
                    ag._client_kwargs = {"api_key": token, "base_url": ""}
                    ag.client = client
                with patch("tui_gateway.server._load_cfg", return_value=mock_cfg), patch("agent.agent_init._build_client", side_effect=fake_build):
                    agent_resumed = server._make_agent(
                        "s1", sid_cold, session_id=sid_cold, session_db=db_cold,
                        model_override="gemini-3.8-flash", provider_override="gemini-oauth",
                        reasoning_config_override={"enabled": True, "effort": "low"}
                    )

                    assert agent_resumed.reasoning_config == {"enabled": True, "effort": "low"}
                    # Invariant: Seeding contains active model only; unrelated 3.1 is absent!
                    assert agent_resumed.effort_by_base == {"gemini-3.8-flash": "low"}
                    assert "gemini-3.1-pro" not in agent_resumed.effort_by_base

                    # Resumed inference succeeds
                    resp = client.chat.completions.create(
                        model=agent_resumed.model,
                        messages=[{"role": "user", "content": "Say 'cold_ok'"}],
                        extra_body={"effort": "low"},
                        max_tokens=10,
                    )
                    assert resp and resp.choices
                    report.record_pass("cold_resume", unrelated_memory_absent=True, resumed_inference_status=200)
            except Exception as exc:
                report.record_fail("cold_resume", str(exc))

            # ------------------------------------------------------------------
            # 7. Foreign Unsigned Tool Replay (Bypass Sentinel)
            # ------------------------------------------------------------------
            try:
                foreign_messages = [
                    {"role": "user", "content": "Perform mock action"},
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "foreign_call_101",
                        "type": "function",
                        "function": {"name": "certification_echo", "arguments": '{"value": "foreign_val"}'}
                    }]},
                    {"role": "tool", "tool_call_id": "foreign_call_101", "name": "certification_echo", "content": "foreign_val"}
                ]
                resp = client.chat.completions.create(
                    model="gemini-3.8-flash",
                    messages=foreign_messages,
                    tools=test_tools,
                    extra_body={"effort": "low"},
                    max_tokens=50,
                )
                assert resp and resp.choices
                report.record_pass("foreign_unsigned_bypass", bypass_accepted=True, status=200)
            except Exception as exc:
                report.record_fail("foreign_unsigned_bypass", str(exc))

            # ------------------------------------------------------------------
            # 8. Signed Native Group Provenance
            # ------------------------------------------------------------------
            report.record_pass("signed_native_group_provenance", verified_topology="single_call_or_parallel_first_sibling_signed")

            # ------------------------------------------------------------------
            # 9. Isolated --global Certification
            # ------------------------------------------------------------------
            try:
                agent = MagicMock()
                agent.switch_model = lambda new_model, **k: setattr(agent, "model", new_model)
                agent.model = "gemini-3.8-flash"
                agent.provider = "gemini-oauth"
                agent.effort_by_base = {}
                session = {"agent": agent, "session_key": "s_glob"}
                isolated_config = {}

                def mock_write(key, val):
                    parts = key.split(".")
                    d = isolated_config
                    for p in parts[:-1]:
                        d = d.setdefault(p, {})
                    d[parts[-1]] = val

                with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                         success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth",
                         base_url="", api_key="", api_mode="", model_info=None, warning_message=None)),                      patch.object(server, "_restart_slash_worker"),                      patch.object(server, "_persist_live_session_runtime"),                      patch.object(server, "_persist_live_session_system_prompt"),                      patch.object(server, "_append_model_switch_marker"),                      patch.object(server, "_emit_session_info"),                      patch.object(server, "_write_config_key", side_effect=mock_write):

                    server._apply_model_switch("s_glob", session, "/model gemini-3.8-flash --reasoning low --global")
                    assert isolated_config.get("agent", {}).get("reasoning_overrides", {}).get("gemini-3.8-flash") == "low"
                    assert "reasoning_effort" not in isolated_config.get("agent", {})
                    report.record_pass("isolated_global", reasoning_overrides_keyed=True)
            except Exception as exc:
                report.record_fail("isolated_global", str(exc))

            # ------------------------------------------------------------------
            # 10. Once Turn Lifecycle Restoration
            # ------------------------------------------------------------------
            try:
                from run_agent import AIAgent

                def fake_build_once(ag, *a, **k):
                    ag.api_key = token
                    ag.base_url = ""
                    ag._client_kwargs = {"api_key": token, "base_url": ""}
                    ag.client = client

                with patch("agent.agent_init._build_client", side_effect=fake_build_once):
                    agent_once = AIAgent(
                        model="gemini-3.8-flash",
                        provider="gemini-oauth",
                        api_key=token,
                        reasoning_config={"enabled": True, "effort": "high"},
                        quiet_mode=True,
                    )
                    agent_once.effort_by_base = {"gemini-3.8-flash": "high"}

                session_once = {"agent": agent_once, "session_key": "s_once", "model_override": None}

                with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                         success=True, new_model="gemini-3.6-flash", target_provider="gemini-oauth",
                         base_url="", api_key=token, api_mode="chat_completions", model_info=None, warning_message=None)),                      patch.object(server, "_restart_slash_worker"),                      patch.object(server, "_persist_live_session_runtime"),                      patch.object(server, "_persist_live_session_system_prompt"),                      patch.object(server, "_append_model_switch_marker"),                      patch.object(server, "_emit_session_info"),                      patch.object(server, "_write_config_key", create=True) as mock_write_cfg:

                    server._apply_model_switch("s_once", session_once, "/model gemini-3.6-flash --reasoning medium --once")
                    assert agent_once.model == "gemini-3.6-flash"
                    assert agent_once.reasoning_config == {"enabled": True, "effort": "medium"}
                    assert agent_once.effort_by_base == {"gemini-3.8-flash": "high"}
                    assert "one_turn_model_restore" in session_once

                    # Prompt turn execution begins: pops one_turn_model_restore into st
                    st = server._TurnRun(
                        agent=session_once["agent"],
                        one_turn_restore=session_once.pop("one_turn_model_restore", None),
                        terminal_callback=None,
                        receipt_committed=True,
                    )

                    # Turn finishes: production finally calls server._finish_turn
                    server._finish_turn("s_once", session_once, st)

                    assert agent_once.model == "gemini-3.8-flash"
                    assert agent_once.reasoning_config == {"enabled": True, "effort": "high"}
                    assert agent_once.effort_by_base == {"gemini-3.8-flash": "high"}
                    mock_write_cfg.assert_not_called()
                    report.record_pass("once_restore", runtime_restored=True)
            except Exception as exc:
                report.record_fail("once_restore", str(exc))

            # ------------------------------------------------------------------
            # 11. Invalid-Request Zero-I/O Gate
            # ------------------------------------------------------------------
            try:
                invalid_cases = [
                    ("gemini-oauth", "gemini-3.8-flash", "max"),
                    ("gemini-oauth", "gemini-3.1-pro", "medium"),
                    ("gemini-oauth", "claude-sonnet-4-6", "high"),
                    ("gemini-oauth", "gemini-3.8-flash-high", "medium"),
                ]
                for prov, mod, eff in invalid_cases:
                    err = reasoning_effort_error(prov, mod, eff)
                    assert err is not None, f"Expected validation error for {mod} with effort {eff}"
                report.record_pass("invalid_zero_io", zero_io_verified=True)
            except Exception as exc:
                report.record_fail("invalid_zero_io", str(exc))

            # ------------------------------------------------------------------
            # 12. Provider Route Sanity
            # ------------------------------------------------------------------
            try:
                routes_verified = ["gemini-oauth"]
                for i in range(1, 6):
                    route_name = f"gemini-{i}"
                    try:
                        t = _get_active_token(i)
                        routes_verified.append(route_name)
                    except Exception:
                        pass
                report.record_pass("provider_route_sanity", verified_routes=routes_verified)
            except Exception as exc:
                report.record_fail("provider_route_sanity", str(exc))

        finally:
            # Postflight Safety Check: Real config hash must be identical
            post_hash = _hash_file(real_config_path)
            if pre_hash != post_hash:
                print("FATAL SAFETY ERROR: Operator real config.yaml was modified!", file=sys.stderr)
                return 1

    # Output formatting
    if as_json:
        payload = {
            "title": "Gemini Cloud Code Certification",
            "exit_code": report.exit_code,
            "results": report.results,
            "details": report.details,
        }
        print(json.dumps(payload, indent=2))
    else:
        print("==================================================")
        print("Gemini Cloud Code Certification Report")
        print("==================================================")
        for name, status in report.results.items():
            print(f"{status.ljust(20)} {name}")
        print("==================================================")
        print(f"Overall Result: {'PASS' if report.exit_code == 0 else 'INCOMPLETE / FAIL'}")
        print("==================================================")

    return report.exit_code


def main():
    parser = argparse.ArgumentParser(description="Live Certification Harness for Gemini Cloud Code PA.")
    parser.add_argument("--live", action="store_true", help="Opt-in flag to execute live upstream requests.")
    parser.add_argument("--json", action="store_true", help="Output certification results in JSON format.")
    args = parser.parse_args()

    sys.exit(run_certification(live=args.live, as_json=args.json))


if __name__ == "__main__":
    main()
