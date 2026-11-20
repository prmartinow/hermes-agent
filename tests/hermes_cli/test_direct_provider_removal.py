"""Direct provider removal preserves generic custom routes and unrelated providers."""

import json

import pytest


@pytest.mark.parametrize("provider", ["gemini-oauth", "gemini-3", "gemini-oauth-5", "gemini-6", "gemini-oauth-99", "solstice"])
def test_removed_provider_cannot_resurrect_from_catalog_or_stale_config(tmp_path, monkeypatch, provider):
    from hermes_cli import auth, models, providers, runtime_provider
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(f"model:\n  provider: {provider}\n  default: gemini-3.7-flash\n  base_url: http://127.0.0.1:18205/v1\n")
    (home / "auth.json").write_text(json.dumps({"version": auth.AUTH_STORE_VERSION, "providers": {
        "gemini-oauth": {"access_token": "synthetic-direct-token"},
    }}))
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-key")
    before = (home / "auth.json").read_bytes()
    monkeypatch.setattr(providers, "_models_dev_info", lambda *args, **kwargs: pytest.fail("removed provider consulted remote catalog"))
    token = set_hermes_home_override(home)
    try:
        assert providers.get_provider(provider) is None
        assert providers.resolve_provider_full(provider, user_providers={provider: {"base_url": "http://127.0.0.1:18205/v1"}}) is None
        assert models.provider_model_ids(provider) == []
        with pytest.raises(auth.AuthError, match="unavailable"):
            runtime_provider.resolve_runtime_provider(requested=provider, explicit_api_key="synthetic-custom-key", explicit_base_url="http://127.0.0.1:18205/v1")
        with pytest.raises(auth.AuthError, match="unavailable"):
            auth.resolve_provider(provider)
        assert (home / "auth.json").read_bytes() == before
    finally:
        reset_hermes_home_override(token)


def test_custom_bridge_route_uses_generic_tool_call_wire():
    from agent.transports.chat_completions import ChatCompletionsTransport
    from hermes_cli import runtime_provider

    runtime = runtime_provider.resolve_runtime_provider(
        requested="custom", explicit_api_key="synthetic-custom-key", explicit_base_url="http://127.0.0.1:18205/v1",
    )
    assert runtime["api_key"] == "synthetic-custom-key"
    assert runtime["base_url"] == "http://127.0.0.1:18205/v1"
    transport = ChatCompletionsTransport()
    signature = {"google": {"thought_signature": "opaque-carrier"}}
    history = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": "call-test", "type": "function", "function": {
            "name": "read_file", "arguments": "{}", "thought_signature": "legacy-signature"
        },
        "extra_content": signature,
    }]}]
    kwargs = transport.build_kwargs(
        model="gemini-3.7-flash", provider_name="custom", base_url=runtime["base_url"],
        messages=history, tools=[], reasoning_config=None,
    )
    assert "extra_content" not in kwargs["messages"][0]["tool_calls"][0]
    assert "thought_signature" not in kwargs["messages"][0]["tool_calls"][0]["function"]
    assert history[0]["tool_calls"][0]["function"]["thought_signature"] == "legacy-signature"
    assert history[0]["tool_calls"][0]["extra_content"] == signature


def test_model_names_pass_through_custom_routes_and_normalize_for_openrouter():
    from hermes_cli.model_normalize import normalize_model_for_provider

    model = "gemini-example"
    assert normalize_model_for_provider(model, "custom") == model
    assert normalize_model_for_provider(model, "openrouter") == "google/" + model
    assert normalize_model_for_provider("google/" + model, "openrouter") == "google/" + model
    assert normalize_model_for_provider("gemma-3", "openrouter") == "google/gemma-3"


def test_vertex_partner_reasoning_remains_available():
    from providers import get_provider_profile
    profile = get_provider_profile("vertex")
    assert profile is not None
    extras = profile.build_extra_body(model="claude-sonnet-4-6", reasoning_config={"enabled": True, "effort": "high"})
    assert extras["extra_body"]["google"]["thinking_config"] == {"include_thoughts": True, "thinking_budget": 16384}


def test_real_entrypoint_imports_and_provider_inventory():
    import run_agent
    import cli
    from hermes_cli import web_server
    from hermes_cli.models_catalog_static import listed_canonical_providers
    from hermes_cli.providers import provider_removed

    assert run_agent.AIAgent and cli.HermesCLI and web_server.app
    assert all(not provider_removed(row.slug) for row in listed_canonical_providers())


@pytest.mark.parametrize("options", [{}, {"include_unconfigured": True}, {"explicit_only": True}])
def test_inventory_does_not_offer_stale_oauth_rows(tmp_path, monkeypatch, options):
    from pathlib import Path
    from agent import models_dev
    from hermes_cli import inventory, model_switch_providers
    from hermes_cli.providers import provider_removed

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-key")
    monkeypatch.setattr(models_dev, "fetch_models_dev", lambda *args, **kwargs: {
        "google": {"id": "google", "name": "Google", "env": ["GOOGLE_API_KEY"],
                   "api": "https://generativelanguage.googleapis.com/v1beta", "models": {
                       "gemini-3.7-flash": {"id": "gemini-3.7-flash", "name": "Synthetic Gemini"},
                   }},
    })
    monkeypatch.setattr(model_switch_providers, "_build_curated_lists", lambda *args, **kwargs: {})
    context = inventory.ConfigContext(
        current_provider="gemini-oauth", current_model="gemini-3.7-flash", current_base_url="",
        user_providers={"gemini-oauth": {"base_url": "http://127.0.0.1:18205/v1", "models": ["gemini-3.7-flash"]}},
        custom_providers=[],
    )
    result = inventory.build_models_payload(
        context, probe_custom_providers=False, non_blocking_catalogs=True, **options,
    )
    assert all(not provider_removed(row["slug"]) for row in result["providers"])
    assert any(row["slug"] == "gemini" for row in result["providers"])


def test_stored_gemini_pool_is_hidden_without_rewriting_credentials(tmp_path):
    from hermes_cli import auth
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"version": auth.AUTH_STORE_VERSION, "credential_pool": {
        "gemini-oauth": [{"id": "synthetic-gemini", "api_key": "synthetic-token"}],
        "openrouter": [{"id": "synthetic-other", "api_key": "synthetic-other-token"}],
    }}))
    before = path.read_bytes()
    token = set_hermes_home_override(tmp_path)
    try:
        assert auth.read_credential_pool("gemini-oauth") == []
        assert "gemini-oauth" not in auth.read_credential_pool()
        assert auth.read_credential_pool("openrouter")[0]["id"] == "synthetic-other"
        assert path.read_bytes() == before
    finally:
        reset_hermes_home_override(token)


def test_vertex_output_headroom_does_not_apply_to_custom_bridge():
    from agent.transports.chat_completions import _provider_output_tokens
    assert _provider_output_tokens("claude-sonnet-4-6", {"enabled": True}, 4096, {"provider_name": "vertex"}) == 64000
    assert _provider_output_tokens("claude-sonnet-4-6", {"enabled": False}, 64, {"provider_name": "vertex"}) == 64
    assert _provider_output_tokens("claude-sonnet-4-6", {"enabled": True}, 4096, {"provider_name": "custom"}) == 4096
