"""Regression & invariant tests for the dashboard chat scrollbar architecture.

Gives a strict binary determination:
- NATIVE SCROLLBAR (Primary buffer, HERMES_TUI_INLINE=1, no AlternateScreen, no TUI box characters)
vs.
- RENDERED SCROLLBAR (AlternateScreen \x1b[?1049h active, TUI box-drawing scrollbar │/┃ active)
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

import hermes_cli.web_server_chat as _web_server_chat


def test_dashboard_chat_launches_with_tui_inline_mode(monkeypatch):
    """Contract test: Verify that _resolve_chat_argv sets HERMES_TUI_INLINE=1 and preserves button mouse tracking."""
    monkeypatch.setattr("hermes_cli.main_tui_launch._make_tui_argv", lambda _root, **_kwargs: (["node"], None))
    _argv, _cwd, env = _web_server_chat._resolve_chat_argv()

    assert env is not None, "chat PTY launch env must not be None"
    assert env.get("HERMES_TUI_INLINE") == "1", (
        "HERMES_TUI_INLINE must be set to '1' so the dashboard chat uses "
        "the native browser scrollbar instead of the TUI-rendered text scrollbar."
    )
    assert env.get("HERMES_TUI_DISABLE_MOUSE") != "1", (
        "HERMES_TUI_DISABLE_MOUSE must not be '1' so click interactivity is preserved."
    )
    assert env.get("HERMES_TUI_MOUSE_TRACKING") == "buttons", (
        "HERMES_TUI_MOUSE_TRACKING must be 'buttons' to enable DEC 1000/1002/1006 click reporting."
    )


def test_dashboard_chat_pty_produces_native_scrollbar_not_rendered(monkeypatch):
    """Protocol test: Spawn the chat PTY and verify it runs in primary buffer mode

    Binary result:
    - PASS: Native scrollbar active (Primary buffer, no \x1b[?1049h, no │/┃ box chars).
    - FAIL: Rendered TUI scrollbar active (AlternateScreen or box-drawing characters detected).
    """
    from hermes_cli.pty_bridge import PtyBridge

    if not PtyBridge.is_available():
        pytest.skip("PTY unavailable")
    tui_dir = Path(__file__).resolve().parents[2] / "ui-tui"
    node = shutil.which("node")
    if not node or not (tui_dir / "dist" / "entry.js").is_file():
        pytest.skip("requires a local Node and a built TUI")
    monkeypatch.setenv("HERMES_NODE", node)
    monkeypatch.setenv("HERMES_TUI_DIR", str(tui_dir))

    argv, cwd, env = _web_server_chat._resolve_chat_argv()
    assert env.get("HERMES_TUI_INLINE") == "1", "HERMES_TUI_INLINE must be 1"

    # Spawn PTY directly via bridge
    bridge = PtyBridge.spawn(argv, cwd=cwd, env=env)
    bridge.resize(cols=80, rows=24)

    buffer = b""
    try:
        # Read initial startup frames (up to 3 seconds)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            chunk = bridge.read(timeout=0.2)
            if chunk:
                buffer += chunk
            if len(buffer) > 1000:
                break
    finally:
        bridge.close()

    assert buffer, "the TUI never wrote to its terminal"
    has_alt_screen = b"\x1b[?1049h" in buffer or b"\x1b[?47h" in buffer
    has_tui_scrollbar = "│".encode() in buffer and "┃".encode() in buffer

    assert not has_alt_screen, (
        "[RENDERED SCROLLBAR DETECTED] PTY emitted AlternateScreen escape (\x1b[?1049h). "
        "Expected native browser scrollbar via primary buffer mode (HERMES_TUI_INLINE=1)."
    )
    assert not has_tui_scrollbar, (
        "[RENDERED SCROLLBAR DETECTED] PTY emitted TUI box-drawing characters (│ and ┃). "
        "Expected native browser scrollbar, not the rendered TUI scrollbar."
    )
