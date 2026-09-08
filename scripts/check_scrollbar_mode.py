#!/usr/bin/env python3
"""Check whether the active dashboard chat uses the Native Scrollbar or the Rendered TUI Scrollbar.

Outputs a clear binary determination:
  [PASS] NATIVE SCROLLBAR IS ACTIVE
  vs.
  [FAIL] RENDERED TUI SCROLLBAR IS ACTIVE
"""

import sys
import time
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import hermes_cli.web_server_chat as _web_server_chat
from hermes_cli.pty_bridge import PtyBridge, PtyUnavailableError


def evaluate_scrollbar_mode():
    argv, cwd, env = _web_server_chat._resolve_chat_argv()
    inline_val = env.get("HERMES_TUI_INLINE") if env else None

    if not PtyBridge.is_available():
        print("[ERROR] PtyBridge is not available on this platform.")
        return 1

    # Spawn test PTY
    bridge = PtyBridge.spawn(argv, cwd=cwd, env=env)
    bridge.resize(cols=80, rows=24)

    buffer = b""
    try:
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

    print("=" * 60)
    print("             HERMES SCROLLBAR ACTIVE MODE CHECK")
    print("=" * 60)
    print(f"  • Launch Env HERMES_TUI_INLINE:          {inline_val}")
    print(f"  • Alternate Screen Escape (\x1b[?1049h): {has_alt_screen}")
    print(f"  • TUI Box Scrollbar Chars (│ and ┃):     {has_tui_scrollbar}")
    print("-" * 60)

    if not has_alt_screen and not has_tui_scrollbar and inline_val == "1":
        print("  [RESULT]: NATIVE SCROLLBAR IS ACTIVE")
        print("  -> Fast browser-native DOM scrollbar is in use.")
        print("  -> Slower/buggy TUI-rendered character scrollbar is NOT active.")
        print("=" * 60)
        return 0
    else:
        print("  [RESULT]: RENDERED TUI SCROLLBAR IS ACTIVE")
        print("  -> Alternate screen mode with TUI box-drawing scrollbar detected.")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(evaluate_scrollbar_mode())
