"""A failed config load hands ``_parse_compression_config`` ``{}``; every key must fall back to
DEFAULT_CONFIG, and an explicit ``threshold_tokens: null`` must stay the ratio-only opt-out."""

from types import SimpleNamespace

import pytest

from agent.agent_init import _parse_compression_config
from hermes_cli.config import DEFAULT_CONFIG


def _agent():
    return SimpleNamespace(model="m", provider="openrouter", api_mode="chat_completions", quiet_mode=True)


@pytest.mark.parametrize(
    ("agent_cfg", "expected"),
    [
        ({}, DEFAULT_CONFIG["compression"]["threshold_tokens"]),  # config-load failure → shipped default
        ({"compression": {"threshold_tokens": None}}, None),  # explicit null → ratio-only opt-out
    ],
)
def test_threshold_tokens_default_and_null_opt_out(agent_cfg, expected):
    cs = _parse_compression_config(_agent(), agent_cfg)
    assert cs.threshold_tokens == expected
    assert cs.threshold == DEFAULT_CONFIG["compression"]["threshold"]


def test_threshold_tokens_exempt_models_default_and_config():
    # Empty config hands default [] -> normalized to ()
    cs_default = _parse_compression_config(_agent(), {})
    assert cs_default.threshold_tokens_exempt_models == ()

    # Explicit exemption models
    cs_exempt = _parse_compression_config(
        _agent(),
        {"compression": {"threshold_tokens_exempt_models": ["astra", "gemini"]}}
    )
    assert cs_exempt.threshold_tokens_exempt_models == ("astra", "gemini")


def test_unsupported_exemption_aliases_ignored():
    # Unsupported alias keys (threshold_tokens_exempt_families, threshold_tokens_exemptions, threshold_tokens_exempt)
    # must NOT be recognized as fallback — only threshold_tokens_exempt_models is supported.
    for alias_key in (
        "threshold_tokens_exempt_families",
        "threshold_tokens_exemptions",
        "threshold_tokens_exempt",
    ):
        cs = _parse_compression_config(
            _agent(),
            {"compression": {alias_key: ["astra", "gemini"]}}
        )
        assert cs.threshold_tokens_exempt_models == ()
