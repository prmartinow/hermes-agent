"""Regression & invariant tests for the dashboard chat scrollbar architecture.

Gives a strict binary determination:
- NATIVE SCROLLBAR (Primary buffer, HERMES_TUI_INLINE=1, no AlternateScreen, no TUI box characters)
vs.
- RENDERED SCROLLBAR (AlternateScreen [?1049h active, TUI box-drawing scrollbar │/┃ active)
"""

from __future__ import annotations

import asyncio
import hermes_cli.web_server_chat as _web_server_chat


def test_dashboard_chat_launches_with_tui_inline_mode():
    """Contract test: Verify that _resolve_chat_argv sets HERMES_TUI_INLINE=1 and preserves button mouse tracking."""
    argv, cwd, env = _web_server_chat._resolve_chat_argv()

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


def test_dashboard_chat_pty_produces_native_scrollbar_not_rendered():
    """Protocol test: Spawn the chat PTY and verify it runs in primary buffer mode

    Binary result:
    - PASS: Native scrollbar active (Primary buffer, no [?1049h, no │/┃ box chars).
    - FAIL: Rendered TUI scrollbar active (AlternateScreen or box-drawing characters detected).
    """
    from hermes_cli.pty_bridge import PtyBridge, PtyUnavailableError

    if PtyBridge is None or not PtyBridge.is_available():
        return

    argv, cwd, env = _web_server_chat._resolve_chat_argv()
    assert env.get("HERMES_TUI_INLINE") == "1", "HERMES_TUI_INLINE must be 1"

    # Spawn PTY directly via bridge
    bridge = PtyBridge.spawn(argv, cwd=cwd, env=env)
    bridge.resize(cols=80, rows=24)

    buffer = b""
    try:
        # Read initial startup frames (up to 3 seconds)
        import time
        start = time.time()
        while time.time() - start < 3.0:
            chunk = bridge.read(timeout=0.2)
            if chunk:
                buffer += chunk
            if len(buffer) > 1000:
                break
    finally:
        bridge.close()

    has_alt_screen = b"[?1049h" in buffer or b"[?47h" in buffer
    has_tui_scrollbar = "│".encode("utf-8") in buffer and "┃".encode("utf-8") in buffer

    assert not has_alt_screen, (
        "[RENDERED SCROLLBAR DETECTED] PTY emitted AlternateScreen escape (\x1b[?1049h). "
        "Expected native browser scrollbar via primary buffer mode (HERMES_TUI_INLINE=1)."
    )
    assert not has_tui_scrollbar, (
        "[RENDERED SCROLLBAR DETECTED] PTY emitted TUI box-drawing characters (│ and ┃). "
        "Expected native browser scrollbar, not the rendered TUI scrollbar."
    )
