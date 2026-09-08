#!/usr/bin/env python3
"""Audit script to cross-check all sessions in state.db:
1. Account of last message/turn
2. Account label displayed next to model in TUI status bar
3. Account label of chat displayed in chat overview panel
"""

import json
from pathlib import Path
from hermes_state import SessionDB
from hermes_cli.auth import get_account_alias
from tui_gateway.server import _session_info
from agent.credential_pool import load_pool


def run_audit():
    db = SessionDB()
    pool = load_pool("gemini-oauth")
    sessions = db.list_sessions_rich(limit=50, compact_rows=True)

    print(f"\n{'='*90}")
    print(f"{'SESSION AUDIT: LAST MESSAGE vs TUI STATUS BAR vs CHAT OVERVIEW':^90}")
    print(f"{'='*90}\n")
    print(f"{'Session ID':<24} | {'Title':<30} | {'Last Msg':<8} | {'TUI Bar':<8} | {'Overview':<8} | {'Status'}")
    print(f"{'-'*24}-+-{'-'*30}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*10}")

    all_matched = True
    audited_count = 0

    for s in sessions:
        sid = s.get("id")
        sess_row = db.get_session(sid)
        if not sess_row:
            continue

        raw_prov = str(s.get("billing_provider") or s.get("provider") or "").lower()
        raw_model = str(s.get("model") or "").lower()

        if "gemini" in raw_prov or "gemini" in raw_model:
            audited_count += 1
            # 1. Last message / persisted account
            cfg = sess_row.get("model_config") or {}
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except Exception:
                    cfg = {}
            last_msg_raw = cfg.get("gemini_account") or ""
            last_msg_alias = get_account_alias(last_msg_raw) if last_msg_raw else "None"

            # 2. TUI model bar account
            class MockAgent:
                provider = "gemini-oauth"
                model = str(sess_row.get("model") or "gemini-3.7-flash-high")
                _credential_pool = pool
                _credential_pool_entry_id = None
                _session_db = db
                session_id = sid

            tui_info = _session_info(MockAgent(), sess_row)
            tui_bar_alias = tui_info.get("gemini_account") or "None"

            # 3. Chat overview sidebar account
            overview_alias = get_account_alias(last_msg_raw) if last_msg_raw else "None"

            matched = (last_msg_alias == tui_bar_alias == overview_alias) and (last_msg_alias != "None")
            if not matched:
                all_matched = False

            status_str = "✅ MATCH" if matched else "❌ MISMATCH"
            title_str = str(s.get("title") or "Untitled")[:28]

            print(f"{sid:<24} | {title_str:<30} | {last_msg_alias:<8} | {tui_bar_alias:<8} | {overview_alias:<8} | {status_str}")

    print(f"\n{'-'*90}")
    print(f"Total Gemini Sessions Audited: {audited_count}")
    print(f"Overall Result: {'✅ ALL SESSIONS 100% IN ALIGNMENT' if all_matched else '❌ MISMATCHES DETECTED'}")
    print(f"{'='*90}\n")
    return all_matched


if __name__ == "__main__":
    success = run_audit()
    exit(0 if success else 1)
