"""Only renderer-only PTYs may retire without a private agent's idle permit."""
import os
import sys

import pytest

from hermes_cli.pty_bridge import PtyBridge


@pytest.mark.skipif(not PtyBridge.is_available(), reason="PTY unavailable")
@pytest.mark.parametrize("attached", [False, True])
def test_retirement_distinguishes_viewer_from_private_compute_owner(attached):
    env = dict(os.environ)
    env["HERMES_TUI_DASHBOARD"] = "1"
    env.pop("HERMES_TUI_GATEWAY_URL", None)
    if attached:
        env["HERMES_TUI_GATEWAY_URL"] = "ws://127.0.0.1:12345/api/ws"
    # A test-owned child only; no production gateway, credentials or agent calls.
    bridge = PtyBridge.spawn([sys.executable, "-c", "import time; time.sleep(30)"], env=env)
    try:
        assert bridge.is_alive()
        assert bridge.retire_if_idle() is attached
        assert bridge.is_alive()  # Approval itself does not send any signal.
        if not attached:
            assert bridge._retirement_socket is not None
    finally:
        bridge.close()


@pytest.mark.skipif(not PtyBridge.is_available(), reason="PTY unavailable")
def test_viewer_retirement_still_requires_exact_process_generation():
    env = {**os.environ, "HERMES_TUI_DASHBOARD": "1",
           "HERMES_TUI_GATEWAY_URL": "ws://127.0.0.1:12345/api/ws"}
    bridge = PtyBridge.spawn([sys.executable, "-c", "import time; time.sleep(30)"], env=env)
    try:
        bridge.process_birth_time = -1
        assert not bridge.retire_if_idle()
        assert bridge.is_alive()
    finally:
        bridge.close()
