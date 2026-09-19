"""Regression for delegate caller/resolver argument compatibility."""
from types import SimpleNamespace

import pytest

from tools.delegate_tool import _resolve_child_runtime


def resolve(**overrides):
    parent = SimpleNamespace(
        model="fixture/model", provider="openrouter", base_url="https://gateway.invalid/v1",
        api_mode="chat_completions", max_tokens=2048,
    )
    params = dict(model=None, override_provider=None, override_base_url=None,
                  override_api_key=None, override_api_mode=None,
                  override_acp_command=None, override_acp_args=None)
    params.update(overrides)
    return _resolve_child_runtime(parent, {}, None, **params)


@pytest.mark.parametrize("override,expected", [(None, 2048), (512, 512)])
def test_explicit_child_output_budget_wins_over_parent(override, expected):
    assert resolve(override_max_tokens=override)["max_tokens"] == expected


def test_unknown_runtime_options_are_not_silently_discarded():
    with pytest.raises(TypeError):
        resolve(misspelled_runtime_option=True)
