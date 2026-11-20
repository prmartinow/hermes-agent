"""The shared legacy Hindsight file must not cross profile boundaries."""

import json
from pathlib import Path

from agent.secret_scope import reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.memory.hindsight import _load_config


def test_routed_profile_does_not_adopt_legacy_config(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    legacy = user_home / ".hindsight" / "config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"apiKey": "launch-only", "mode": "cloud"}), encoding="utf-8")
    secondary = tmp_path / "secondary"
    secondary.mkdir()
    monkeypatch.setattr(Path, "home", lambda: user_home)
    monkeypatch.setenv("HINDSIGHT_API_KEY", "launch-env")
    multiplex_token = set_multiplex_context(True)
    home_token = set_hermes_home_override(str(secondary))
    secret_token = set_secret_scope({"HINDSIGHT_API_KEY": "secondary-key"}, profile_home=str(secondary))
    try:
        config = _load_config()
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
        reset_multiplex_context(multiplex_token)
    assert config["apiKey"] == "secondary-key"


def test_shared_async_loop_uses_each_callers_profile(tmp_path):
    from agent.runtime_scope import bind_state, scope_for_profile
    from agent.secret_scope import get_secret_str
    from hermes_constants import get_hermes_home
    from plugins.memory.hindsight import _run_sync

    async def observe_profile():
        return get_hermes_home(), get_secret_str("HINDSIGHT_API_KEY")

    for name in ("first", "second", "first"):
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        with bind_state(scope_for_profile(home), secrets={"HINDSIGHT_API_KEY": f"{name}-key"}):
            assert _run_sync(observe_profile()) == (home, f"{name}-key")
