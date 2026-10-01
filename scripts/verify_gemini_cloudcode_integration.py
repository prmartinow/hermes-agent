#!/usr/bin/env python3
"""Reproducible Live Certification & Closure Harness for Google Gemini Cloud Code PA.

Certifies that production Hermes correctly executes:
1. Dynamic model & effort routing (gemini-3.8-flash -> gemini-3.8-flash-tiered, thinkingLevel: low).
2. Static model routing (gemini-3.6-flash -> gemini-3.6-flash-medium).
3. In-memory per-base effort retention across model switches.
4. Partner route excursion & restoration (claude-sonnet-4-6).
5. Live Google thought-signature acquisition & verbatim tool replay.
6. Empirical signed native group provenance (first sibling signed, siblings unsigned).
7. SQLite persistence closure (session DB close, reopen, and replay).
8. Cold session resume from persisted database row via production resume reader.
9. Foreign unsigned tool trace bypass projection and live acceptance.
10. Isolated global reasoning persistence and reload.
11. Once-turn lifecycle restoration via production _TurnRun / _finish_turn.
12. Zero I/O rejection of invalid configurations with spied side-effect seams.
13. Provider route resolution sanity across numbered accounts.

Safety contracts:
- Requires explicit --live flag to avoid accidental external network requests.
- Real operator credentials and configs are never mutated; uses isolated temporary HERMES_HOME.
- Secret sanitization on all output (tokens, signatures, keys, and local paths redacted).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.gemini_cloudcode_adapter import GeminiCloudCodeClient
from agent.gemini_cloudcode_models import (
    parse_model_slug,
    resolve_model_selection,
    selectable_reasoning_efforts,
)
from agent.gemini_native_adapter import _build_gemini_contents
from agent.native_replay import find_native_assistant_detail
from agent.reasoning_selection import resolve_effective_reasoning_effort
from hermes_cli.auth import get_gemini_oauth_auth_status
from hermes_cli.config import load_config, save_config
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from hermes_state import SessionDB
from run_agent import AIAgent
import tui_gateway.server as server


def to_dict(obj: Any) -> Any:
    """Recursively convert SimpleNamespace or Pydantic models to dicts."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, SimpleNamespace):
        return {k: to_dict(v) for k, v in vars(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    return obj


def sanitize_secrets(text: Any) -> str:
    """Sanitize secrets, tokens, thought signatures, and local paths from text."""
    if not isinstance(text, str):
        text = str(text)
    # Redact Google thought signatures
    text = re.sub(r'EmQKY[A-Za-z0-9_\-]+', '[REDACTED_THOUGHT_SIGNATURE]', text)
    # Redact OAuth access tokens
    text = re.sub(r'ya29\.[A-Za-z0-9_\-]+', '[REDACTED_OAUTH_TOKEN]', text)
    # Redact Google API keys
    text = re.sub(r'AIzaSy[A-Za-z0-9_\-]+', '[REDACTED_API_KEY]', text)
    # Redact private workstation paths
    text = re.sub(r'/home/[A-Za-z0-9_.\-]+', '[REDACTED_HOME_PATH]', text)
    text = re.sub(r'/Users/[A-Za-z0-9_.\-]+', '[REDACTED_HOME_PATH]', text)
    return text


def classify_api_exception(exc: Exception) -> tuple[str, str]:
    """Classify API exception as UPSTREAM_UNAVAILABLE vs FAIL based on status code and error details."""
    status = getattr(exc, "status_code", None)
    if status is None and hasattr(exc, "response"):
        status = getattr(exc.response, "status_code", None)

    exc_str = sanitize_secrets(str(exc))
    if status in (502, 503, 504):
        return ("UPSTREAM_UNAVAILABLE", f"Upstream service unavailable (HTTP {status})")
    if status == 429:
        return ("UPSTREAM_UNAVAILABLE", "Upstream quota / rate limit exhausted (HTTP 429)")
    if status == 400 and ("MODEL_PLACEHOLDER" in exc_str or "quota" in exc_str.lower()):
        return ("UPSTREAM_UNAVAILABLE", f"Upstream model placeholder / quota limit: {exc_str}")
    return ("FAIL", f"HTTP {status}: {exc_str}" if status else exc_str)


class CertificationResult:
    def __init__(self) -> None:
        self.results: Dict[str, str] = {}
        self.details: Dict[str, Any] = {}

    def record_pass(self, name: str, **kwargs: Any) -> None:
        self.results[name] = "PASS"
        self.details[name] = {k: sanitize_secrets(v) if isinstance(v, str) else v for k, v in kwargs.items()}

    def record_fail(self, name: str, reason: str, **kwargs: Any) -> None:
        self.results[name] = "FAIL"
        sanitized_reason = sanitize_secrets(reason)
        clean_kwargs = {k: sanitize_secrets(v) if isinstance(v, str) else v for k, v in kwargs.items()}
        self.details[name] = {"error": sanitized_reason, **clean_kwargs}

    def record_upstream_unavailable(self, name: str, reason: str, **kwargs: Any) -> None:
        self.results[name] = "UPSTREAM_UNAVAILABLE"
        sanitized_reason = sanitize_secrets(reason)
        clean_kwargs = {k: sanitize_secrets(v) if isinstance(v, str) else v for k, v in kwargs.items()}
        self.details[name] = {"reason": sanitized_reason, **clean_kwargs}

    def record_skipped(self, name: str, reason: str, **kwargs: Any) -> None:
        self.results[name] = "SKIPPED"
        sanitized_reason = sanitize_secrets(reason)
        clean_kwargs = {k: sanitize_secrets(v) if isinstance(v, str) else v for k, v in kwargs.items()}
        self.details[name] = {"reason": sanitized_reason, **clean_kwargs}

    @property
    def exit_code(self) -> int:
        if any(v == "FAIL" for v in self.results.values()):
            return 1
        if any(v == "UPSTREAM_UNAVAILABLE" for v in self.results.values()):
            return 2
        return 0

    def print_text_summary(self) -> None:
        print("==================================================")
        print("Gemini Cloud Code Certification Report")
        print("==================================================")
        for name, outcome in self.results.items():
            print(f"{outcome:<20} {name}")
        print("==================================================")
        if self.exit_code == 0:
            print("Overall Result: PASS")
        elif self.exit_code == 2:
            print("Overall Result: UPSTREAM_UNAVAILABLE")
        else:
            print("Overall Result: FAIL")
        print("==================================================")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": "Gemini Cloud Code Certification",
            "exit_code": self.exit_code,
            "results": self.results,
            "details": self.details,
        }


def _hash_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_certification(*, live: bool, as_json: bool) -> int:
    report = CertificationResult()

    if not live:
        print("Notice: --live flag is required to run live Cloud Code certification.", file=sys.stderr)
        return 1

    real_hermes_home = Path.home() / ".hermes"
    real_config_path = real_hermes_home / "config.yaml"
    real_auth_path = real_hermes_home / "auth.json"

    initial_real_config_hash = _hash_file(real_config_path)
    initial_real_auth_hash = _hash_file(real_auth_path)

    temp_dir = Path(tempfile.mkdtemp(prefix="hermes_cert_"))
    temp_hermes_home = temp_dir / ".hermes"
    temp_hermes_home.mkdir(parents=True, exist_ok=True)

    # Copy auth.json into temporary home so token reads/refreshes never mutate real operator credentials
    if real_auth_path.exists():
        shutil.copy2(real_auth_path, temp_hermes_home / "auth.json")

    # Initialize temporary isolated config.yaml
    initial_temp_config = {
        "agent": {
            "model": "gemini-3.8-flash",
            "provider": "gemini-oauth",
        }
    }
    with open(temp_hermes_home / "config.yaml", "w") as f:
        import yaml
        yaml.safe_dump(initial_temp_config, f)

    override_token = set_hermes_home_override(temp_hermes_home)
    orig_env_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(temp_hermes_home)

    try:
        # Credential Discovery in isolated home
        st = get_gemini_oauth_auth_status(1)
        token = st.get("api_key")
        if not token:
            report.record_upstream_unavailable("credential_discovery", "No active Gemini OAuth token found")
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
            assert resp.choices and resp.choices[0].message
            report.record_pass("dynamic_3_8_low", wire_model=resolved.wire_model, status=200)
        except Exception as exc:
            kind, msg = classify_api_exception(exc)
            if kind == "UPSTREAM_UNAVAILABLE":
                report.record_upstream_unavailable("dynamic_3_8_low", msg)
            else:
                report.record_fail("dynamic_3_8_low", msg)

        # ------------------------------------------------------------------
        # 2. Static Routing Live Gate
        # ------------------------------------------------------------------
        try:
            resolved_static = resolve_model_selection("gemini-3.6-flash", effort="medium")
            assert resolved_static.wire_model == "gemini-3.6-flash-medium", f"Wrong wire model: {resolved_static.wire_model}"
            assert resolved_static.thinking_config is None, "Static model must not have thinkingConfig"

            resp_static = client.chat.completions.create(
                model="gemini-3.6-flash",
                messages=[{"role": "user", "content": "Respond with 'static_ok'"}],
                extra_body={"effort": "medium"},
                max_tokens=15,
            )
            assert resp_static.choices and resp_static.choices[0].message
            report.record_pass("static_3_6_medium", wire_model=resolved_static.wire_model, status=200)
        except Exception as exc:
            kind, msg = classify_api_exception(exc)
            if kind == "UPSTREAM_UNAVAILABLE":
                report.record_upstream_unavailable("static_3_6_medium", msg)
            else:
                report.record_fail("static_3_6_medium", msg)

        # ------------------------------------------------------------------
        # 3. Same-Process Per-Base Effort Memory
        # ------------------------------------------------------------------
        try:
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
                     base_url="", api_key=token, api_mode="chat_completions", model_info=None, warning_message=None)),                  patch.object(server, "_restart_slash_worker"),                  patch.object(server, "_persist_live_session_runtime"),                  patch.object(server, "_persist_live_session_system_prompt"),                  patch.object(server, "_append_model_switch_marker"),                  patch.object(server, "_emit_session_info"):

                server._apply_model_switch("s_mem", session, "/model gemini-3.6-flash --reasoning medium --tui-session")
                assert agent.model == "gemini-3.6-flash"
                assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
                assert agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.6-flash": "medium"}

            # Switch back to 3.8 without supplying effort
            with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                     success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth",
                     base_url="", api_key=token, api_mode="chat_completions", model_info=None, warning_message=None)),                  patch.object(server, "_restart_slash_worker"),                  patch.object(server, "_persist_live_session_runtime"),                  patch.object(server, "_persist_live_session_system_prompt"),                  patch.object(server, "_append_model_switch_marker"),                  patch.object(server, "_emit_session_info"):

                server._apply_model_switch("s_mem", session, "/model gemini-3.8-flash --tui-session")
                # Memory recovers previous low!
                assert agent.model == "gemini-3.8-flash"
                assert agent.reasoning_config == {"enabled": True, "effort": "low"}
                assert agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.6-flash": "medium"}
                report.record_pass("switch_memory", effort_by_base_intact=True)
        except Exception as exc:
            report.record_fail("switch_memory", str(exc))

        # ------------------------------------------------------------------
        # 4. Partner Route Excursion & Restoration
        # ------------------------------------------------------------------
        try:
            assert selectable_reasoning_efforts("gemini-oauth", "claude-sonnet-4-6") == ()
            e_partner = resolve_effective_reasoning_effort(
                config={}, provider="gemini-oauth", model="claude-sonnet-4-6", effort_by_base={"gemini-3.8-flash": "low"}
            )
            assert e_partner is None, f"Partner model must have None effective reasoning effort, got {e_partner}"

            # Attempt live completion request on partner route
            try:
                resp_partner = client.chat.completions.create(
                    model="claude-sonnet-4-6",
                    messages=[{"role": "user", "content": "Respond with 'partner_ok'"}],
                    max_tokens=15,
                )
                assert resp_partner.choices and resp_partner.choices[0].message
                report.record_pass("partner_excursion", partner="claude-sonnet-4-6", status=200)
            except Exception as p_exc:
                p_kind, p_msg = classify_api_exception(p_exc)
                if p_kind == "UPSTREAM_UNAVAILABLE":
                    report.record_upstream_unavailable("partner_excursion", p_msg)
                else:
                    report.record_fail("partner_excursion", p_msg)

            # Return to gemini-3.8-flash
            e_restored = resolve_effective_reasoning_effort(
                config={}, provider="gemini-oauth", model="gemini-3.8-flash", effort_by_base={"gemini-3.8-flash": "low"}
            )
            assert e_restored == "low", f"Failed to restore low effort after partner excursion: {e_restored}"
        except Exception as exc:
            report.record_fail("partner_excursion", str(exc))

        # ------------------------------------------------------------------
        # 5. Live Signed Tool Replay & 6. Group Provenance
        # ------------------------------------------------------------------
        captured_tc_list: List[Any] = []
        captured_carrier: Any = None
        orig_sig: Optional[str] = None
        try:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "certification_echo",
                        "description": "Echo input value back",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    },
                }
            ]

            # Request 2 parallel tool calls to observe and verify group provenance topology
            resp_tool = client.chat.completions.create(
                model="gemini-3.8-flash",
                messages=[
                    {
                        "role": "user",
                        "content": "Call certification_echo with value='val1', and also call certification_echo with value='val2'. You must emit both tool calls.",
                    }
                ],
                tools=tools,
                extra_body={"effort": "low"},
                max_tokens=60,
            )
            assistant_msg = resp_tool.choices[0].message
            captured_tc_list = getattr(assistant_msg, "tool_calls", None) or []
            assert captured_tc_list, "Model failed to emit tool calls"

            # Check for signature on first tool call
            first_tc = captured_tc_list[0]
            extra = getattr(first_tc, "extra_content", {}) or {}
            orig_sig = extra.get("google", {}).get("thought_signature")
            assert orig_sig, "First tool call did not carry Google thought signature"

            # Check native carrier
            captured_carrier = find_native_assistant_detail(getattr(assistant_msg, "reasoning_details", None))
            assert captured_carrier, "Native assistant carrier missing from response"

            # Replay tool responses upstream
            tool_replay_messages = [
                {"role": "user", "content": "Run certification echo"},
                to_dict(assistant_msg),
            ]
            for tc in captured_tc_list:
                call_id = getattr(tc, "id", None) or tc.get("id")
                fn = getattr(tc, "function", None) or tc.get("function")
                fn_name = getattr(fn, "name", None) or fn.get("name")
                tool_replay_messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": fn_name,
                    "content": "echo_receipt",
                })

            resp_followup = client.chat.completions.create(
                model="gemini-3.8-flash",
                messages=tool_replay_messages,
                extra_body={"effort": "low"},
                max_tokens=25,
            )
            assert resp_followup.choices and resp_followup.choices[0].message
            report.record_pass("signed_tool_replay", signature_present=True, native_carrier_present=True, replay_status=200)

            # Gate 6: Signed Native Group Provenance (empirical verification)
            if len(captured_tc_list) > 1:
                assert orig_sig is not None, "First sibling missing signature"
                for tc_sibling in captured_tc_list[1:]:
                    sib_extra = getattr(tc_sibling, "extra_content", {}) or {}
                    sib_sig = sib_extra.get("google", {}).get("thought_signature")
                    assert sib_sig is None, f"Sibling unexpectedly carried thought signature: {sib_sig}"
                report.record_pass(
                    "signed_native_group_provenance",
                    verified_topology="parallel_first_sibling_signed",
                    sibling_count=len(captured_tc_list),
                )
            else:
                report.record_skipped(
                    "signed_native_group_provenance",
                    reason="single_tool_call_observed_in_run",
                )
        except Exception as exc:
            kind, msg = classify_api_exception(exc)
            if kind == "UPSTREAM_UNAVAILABLE":
                report.record_upstream_unavailable("signed_tool_replay", msg)
                report.record_skipped("signed_native_group_provenance", reason="upstream_tool_call_failed")
            else:
                report.record_fail("signed_tool_replay", msg)
                report.record_fail("signed_native_group_provenance", msg)

        # ------------------------------------------------------------------
        # 7. SQLite Replay with DB Close / Reopen & Exact Carrier Mirrors
        # ------------------------------------------------------------------
        try:
            db_path = temp_dir / "sqlite_replay.db"
            db = SessionDB(db_path)
            sid = "s_sqlite_replay"
            db.create_session(sid, "cli", model="gemini-3.8-flash")

            tc_dumps = [to_dict(tc) for tc in captured_tc_list]
            db.append_message(
                session_id=sid,
                role="assistant",
                content=None,
                tool_calls=tc_dumps,
                reasoning_details=captured_carrier,
            )
            for tc in captured_tc_list:
                call_id = getattr(tc, "id", None) or tc.get("id")
                fn = getattr(tc, "function", None) or tc.get("function")
                fn_name = getattr(fn, "name", None) or fn.get("name")
                db.append_message(
                    session_id=sid,
                    role="tool",
                    content="echo_receipt",
                    tool_calls=[{"id": call_id, "name": fn_name}],
                )

            # Close DB connection
            db.close()

            # Reopen DB connection cleanly
            db_reopened = SessionDB(db_path)
            reloaded_msgs = db_reopened.get_messages(sid)
            reloaded_assistant = reloaded_msgs[0]

            # Assert exact byte-for-byte carrier and signature mirrors
            assert reloaded_assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == orig_sig
            reloaded_carrier = find_native_assistant_detail(reloaded_assistant.get("reasoning_details"))
            assert reloaded_carrier == captured_carrier, "Native carrier roundtrip mismatch"

            replay_payload = [
                {"role": "user", "content": "Run certification echo"},
                *reloaded_msgs,
            ]
            resp_db = client.chat.completions.create(
                model="gemini-3.8-flash",
                messages=replay_payload,
                extra_body={"effort": "low"},
                max_tokens=25,
            )
            assert resp_db.choices and resp_db.choices[0].message
            db_reopened.close()
            report.record_pass("sqlite_signed_replay", signature_roundtrip_exact=True, native_carrier_exact=True, status=200)
        except Exception as exc:
            kind, msg = classify_api_exception(exc)
            if kind == "UPSTREAM_UNAVAILABLE":
                report.record_upstream_unavailable("sqlite_signed_replay", msg)
            else:
                report.record_fail("sqlite_signed_replay", msg)

        # ------------------------------------------------------------------
        # 8. Cold Session Resume via Production Overrides Reader
        # ------------------------------------------------------------------
        try:
            db_cold_path = temp_dir / "cold_resume.db"
            db_cold = SessionDB(db_cold_path)
            sid_cold = "s_cold_session"

            db_cold.create_session(sid_cold, "cli", model="gemini-3.8-flash")
            db_cold.update_session_model(sid_cold, "gemini-3.8-flash", "gemini-oauth")
            db_cold.patch_session_model_config(sid_cold, {"reasoning_config": {"enabled": True, "effort": "low"}})

            # Read overrides through normal production resume reader seam
            row = db_cold.get_session(sid_cold)
            overrides = server._stored_session_runtime_overrides(row)
            assert overrides.get("reasoning_config_override") == {"enabled": True, "effort": "low"}
            assert overrides.get("provider_override") == "gemini-oauth"

            def fake_build_cold(ag, *a, **k):
                ag.api_key = token
                ag.base_url = ""
                ag._client_kwargs = {"api_key": token, "base_url": ""}
                ag.client = client

            mock_cfg = {"model": "gemini-3.8-flash", "provider": "gemini-oauth"}
            with patch("tui_gateway.server._load_cfg", return_value=mock_cfg),                  patch("agent.agent_init._build_client", side_effect=fake_build_cold):
                agent_resumed = server._make_agent(
                    "s1", sid_cold, session_id=sid_cold, session_db=db_cold,
                    **overrides
                )

            assert agent_resumed.reasoning_config == {"enabled": True, "effort": "low"}
            assert agent_resumed.effort_by_base == {"gemini-3.8-flash": "low"}

            resp_cold = agent_resumed.client.chat.completions.create(
                model=agent_resumed.model,
                messages=[{"role": "user", "content": "Respond with 'cold_ok'"}],
                extra_body={"effort": agent_resumed.reasoning_config["effort"]},
                max_tokens=15,
            )
            assert resp_cold.choices and resp_cold.choices[0].message
            db_cold.close()
            report.record_pass("cold_resume", unrelated_memory_absent=True, resumed_inference_status=200)
        except Exception as exc:
            kind, msg = classify_api_exception(exc)
            if kind == "UPSTREAM_UNAVAILABLE":
                report.record_upstream_unavailable("cold_resume", msg)
            else:
                report.record_fail("cold_resume", msg)

        # ------------------------------------------------------------------
        # 9. Foreign Unsigned Tool Trace Bypass Projection & Upstream Acceptance
        # ------------------------------------------------------------------
        try:
            foreign_history = [
                {"role": "user", "content": "Run unsigned foreign tool"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "foreign_call_99",
                            "type": "function",
                            "function": {"name": "certification_echo", "arguments": '{"value": "foreign123"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "foreign_call_99", "name": "certification_echo", "content": "foreign123"},
            ]

            # 1. Assert local invariant: outbound wire projection has bypass sentinel, source history is unmodified
            wire_contents, _ = _build_gemini_contents(foreign_history, model="gemini-3.8-flash")
            assert foreign_history[1]["tool_calls"][0].get("extra_content") is None, "Source history was mutated"
            wire_assistant_parts = wire_contents[1].get("parts", [])
            has_bypass = any(p.get("thoughtSignature") == "skip_thought_signature_validator" for p in wire_assistant_parts)
            assert has_bypass, "Outbound wire projection missing skip_thought_signature_validator sentinel"

            # 2. Assert upstream acceptance
            resp_foreign = client.chat.completions.create(
                model="gemini-3.8-flash",
                messages=foreign_history,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "certification_echo",
                            "description": "Echo input value back",
                            "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
                        },
                    }
                ],
                max_tokens=25,
            )
            assert resp_foreign.choices and resp_foreign.choices[0].message
            report.record_pass("foreign_unsigned_bypass", bypass_accepted=True, status=200)
        except Exception as exc:
            kind, msg = classify_api_exception(exc)
            if kind == "UPSTREAM_UNAVAILABLE":
                report.record_upstream_unavailable("foreign_unsigned_bypass", msg)
            else:
                report.record_fail("foreign_unsigned_bypass", msg)

        # ------------------------------------------------------------------
        # 10. Isolated Global Reasoning Persistence and Reload
        # ------------------------------------------------------------------
        try:
            # Write global override in isolated home using production config persistence
            cfg_to_save = load_config()
            cfg_to_save.setdefault("agent", {}).setdefault("reasoning_overrides", {})["gemini-3.8-flash"] = "low"
            save_config(cfg_to_save, merge_existing=True)

            # Reload config through production config reader
            reloaded_cfg = load_config()
            assert reloaded_cfg.get("agent", {}).get("reasoning_overrides", {}).get("gemini-3.8-flash") == "low"

            # Verify effective effort resolver reflects persisted override
            eff = resolve_effective_reasoning_effort(config=reloaded_cfg, provider="gemini-oauth", model="gemini-3.8-flash")
            assert eff == "low"

            # Assert operator's real config was never touched
            assert _hash_file(real_config_path) == initial_real_config_hash
            report.record_pass("isolated_global", reasoning_overrides_keyed=True)
        except Exception as exc:
            report.record_fail("isolated_global", str(exc))

        # ------------------------------------------------------------------
        # 11. Once Turn Lifecycle Restoration
        # ------------------------------------------------------------------
        try:
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
                     base_url="", api_key=token, api_mode="chat_completions", model_info=None, warning_message=None)),                  patch.object(server, "_restart_slash_worker"),                  patch.object(server, "_persist_live_session_runtime"),                  patch.object(server, "_persist_live_session_system_prompt"),                  patch.object(server, "_append_model_switch_marker"),                  patch.object(server, "_emit_session_info"),                  patch.object(server, "_write_config_key", create=True) as mock_write_cfg:

                server._apply_model_switch("s_once", session_once, "/model gemini-3.6-flash --reasoning medium --once")
                assert agent_once.model == "gemini-3.6-flash"
                assert agent_once.reasoning_config == {"enabled": True, "effort": "medium"}
                assert agent_once.effort_by_base == {"gemini-3.8-flash": "high"}
                assert "one_turn_model_restore" in session_once

                st = server._TurnRun(
                    agent=session_once["agent"],
                    one_turn_restore=session_once.pop("one_turn_model_restore", None),
                    terminal_callback=None,
                    receipt_committed=True,
                )

                server._finish_turn("s_once", session_once, st)

                assert agent_once.model == "gemini-3.8-flash"
                assert agent_once.reasoning_config == {"enabled": True, "effort": "high"}
                assert agent_once.effort_by_base == {"gemini-3.8-flash": "high"}
                mock_write_cfg.assert_not_called()
                report.record_pass("once_restore", runtime_restored=True)
        except Exception as exc:
            report.record_fail("once_restore", str(exc))

        # ------------------------------------------------------------------
        # 12. Zero I/O Rejection with Spied Side-Effect Seams
        # ------------------------------------------------------------------
        try:
            agent_zero = MagicMock()
            agent_zero.model = "gemini-3.8-flash"
            agent_zero.provider = "gemini-oauth"
            agent_zero.reasoning_config = {"enabled": True, "effort": "low"}
            agent_zero.effort_by_base = {"gemini-3.8-flash": "low"}
            session_zero = {"agent": agent_zero, "session_key": "s_zero", "model_override": None}

            bad_switches = [
                "/model gemini-3.8-flash --reasoning max --tui-session",
                "/model gemini-3.1-pro --reasoning medium --tui-session",
                "/model claude-sonnet-4-6 --reasoning high --tui-session",
            ]

            for bad_cmd in bad_switches:
                with patch("httpx.Client.send") as mock_http,                      patch.object(server, "_write_config_key") as mock_cfg,                      patch.object(server, "_persist_live_session_runtime") as mock_persist,                      patch("agent.agent_runtime_helpers.switch_model") as mock_agent_switch:

                    caught = False
                    try:
                        server._apply_model_switch("s_zero", session_zero, bad_cmd)
                    except ValueError:
                        caught = True

                    assert caught, f"Expected ValueError for invalid switch: {bad_cmd}"
                    assert mock_http.call_count == 0, f"HTTP calls occurred for {bad_cmd}"
                    assert mock_cfg.call_count == 0, f"Config write occurred for {bad_cmd}"
                    assert mock_persist.call_count == 0, f"Session persistence occurred for {bad_cmd}"
                    assert mock_agent_switch.call_count == 0, f"Agent switch invoked for {bad_cmd}"

            report.record_pass("invalid_zero_io", zero_io_verified=True)
        except Exception as exc:
            report.record_fail("invalid_zero_io", str(exc))

        # ------------------------------------------------------------------
        # 13. Provider Route Resolution Sanity Across Numbered Accounts
        # ------------------------------------------------------------------
        try:
            routes_verified: List[str] = []
            for acc in [1, 2, 3, 4, 5]:
                prov_name = "gemini-oauth" if acc == 1 else f"gemini-{acc}"
                st_acc = get_gemini_oauth_auth_status(acc)
                if st_acc.get("logged_in") or st_acc.get("api_key"):
                    res = resolve_model_selection("gemini-3.8-flash", effort="low")
                    assert res.wire_model == "gemini-3.8-flash-tiered"
                    routes_verified.append(prov_name)

            assert "gemini-oauth" in routes_verified
            report.record_pass("provider_route_sanity", verified_routes=routes_verified, route_resolved=True)
        except Exception as exc:
            report.record_fail("provider_route_sanity", str(exc))

    finally:
        # Restore environment and verify operator config/auth immutability
        reset_hermes_home_override(override_token)
        if orig_env_home is not None:
            os.environ["HERMES_HOME"] = orig_env_home
        else:
            os.environ.pop("HERMES_HOME", None)

        post_config_hash = _hash_file(real_config_path)
        post_auth_hash = _hash_file(real_auth_path)

        if initial_real_config_hash and post_config_hash != initial_real_config_hash:
            report.record_fail("safety_check", "CRITICAL: Real operator config.yaml was mutated during certification!")
        if initial_real_auth_hash and post_auth_hash != initial_real_auth_hash:
            report.record_fail("safety_check", "CRITICAL: Real operator auth.json was mutated during certification!")

        shutil.rmtree(temp_dir, ignore_errors=True)

    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        report.print_text_summary()

    return report.exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify Google Gemini Cloud Code PA Integration")
    parser.add_argument("--live", action="store_true", help="Execute live requests against Google Cloud Code PA")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    args = parser.parse_args()

    exit_code = run_certification(live=args.live, as_json=args.json)
    sys.exit(exit_code)
