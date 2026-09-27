"""Tests for auxiliary timeout propagation across primary, retry, and fallback paths.

Covers:
1. Preservation of auxiliary.<task>.no_progress_timeout across:
   - Initial call (_prepare_aux_request / call_llm / async_call_llm)
   - Same-provider retry (_prepare_same_provider_retry / sync & async)
   - Fallback candidate and auth rebuild (_fallback_request_kwargs / sync & async)
2. Proper gating to Codex Responses-API adapters without injecting into non-Codex providers.
3. No leakage across tasks (e.g. compression override does not affect title_generation).
4. Default behavior when no_progress_timeout is unset.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import yaml

from hermes_cli import config as hermes_config
from agent import auxiliary_client as aux


def _make_mock_codex_client():
    mock_real_client = SimpleNamespace(
        api_key="mock_key",
        base_url="https://chatgpt.com/backend-api/codex/",
        close=lambda: None,
        responses=SimpleNamespace(create=MagicMock(side_effect=RuntimeError("stop_stream"))),
    )
    return aux.CodexAuxiliaryClient(mock_real_client, "gpt-6-astra-900k")


class TestAuxCompressionTimeoutPropagation:
    def test_explicit_compression_timeout_propagates_primary_retry_fallback(self):
        """When auxiliary.compression.no_progress_timeout is configured (180s),
        it propagates to primary, retry, and fallback kwargs and guards."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            hermes_home = os.path.join(tmp_dir, ".hermes")
            os.makedirs(hermes_home, exist_ok=True)
            config_path = os.path.join(hermes_home, "config.yaml")

            cfg_data = {
                "auxiliary": {
                    "compression": {
                        "no_progress_timeout": 180,
                    }
                }
            }
            with open(config_path, "w") as f:
                yaml.safe_dump(cfg_data, f)

            with patch.dict(os.environ, {"HERMES_HOME": hermes_home}):
                hermes_config._LOAD_CONFIG_CACHE.clear()

                timeout_val = aux._get_task_no_progress_timeout("compression")
                assert timeout_val == 180.0

                mock_aux_client = _make_mock_codex_client()

                # 1. Primary path
                with (
                    patch.object(aux, "_resolve_call_client",
                                 return_value=(mock_aux_client, "gpt-6-astra-900k", "openai-codex", "openai-codex")),
                    patch.object(aux, "_resolve_task_provider_model",
                                 return_value=("openai-codex", "gpt-6-astra-900k", "https://chatgpt.com/backend-api/codex/", "mock_key", None)),
                ):
                    prep = aux._prepare_aux_request(
                        "compression",
                        provider="openai-codex",
                        model="gpt-6-astra-900k",
                        base_url="https://chatgpt.com/backend-api/codex/",
                        api_key="mock_key",
                        main_runtime={},
                        messages=[{"role": "user", "content": "compress this"}],
                        temperature=None,
                        max_tokens=None,
                        tools=None,
                        timeout=300.0,
                        extra_body={},
                        reasoning_config=None,
                        extra_headers=None,
                        api_mode=None,
                        route_info=None,
                        async_mode=False,
                    )
                primary_kwargs = prep.kwargs
                assert primary_kwargs.get("no_progress_timeout") == 180.0
                primary_guard = aux._CodexStreamGuard(
                    mock_aux_client._real_client,
                    total_timeout=300.0,
                    no_progress_timeout=primary_kwargs.get("no_progress_timeout"),
                )
                assert primary_guard.no_progress_timeout == 180.0

                # 2. Retry path (same-provider retry)
                with patch.object(aux, "_get_cached_client", return_value=(mock_aux_client, "gpt-6-astra-900k")):
                    retry_client, retry_kwargs = aux._prepare_same_provider_retry(
                        task="compression",
                        resolved_provider="openai-codex",
                        resolved_model="gpt-6-astra-900k",
                        resolved_base_url="https://chatgpt.com/backend-api/codex/",
                        resolved_api_key="mock_key",
                        resolved_api_mode=None,
                        main_runtime={"provider": "openai-codex", "model": "gpt-6-astra-900k"},
                        final_model="gpt-6-astra-900k",
                        messages=[{"role": "user", "content": "retry compress"}],
                        temperature=None,
                        max_tokens=None,
                        tools=None,
                        effective_timeout=300.0,
                        effective_extra_body={},
                        reasoning_config=None,
                        async_mode=False,
                    )

                assert retry_kwargs.get("no_progress_timeout") == 180.0
                retry_guard = aux._CodexStreamGuard(
                    retry_client._real_client,
                    total_timeout=300.0,
                    no_progress_timeout=retry_kwargs.get("no_progress_timeout"),
                )
                assert retry_guard.no_progress_timeout == 180.0

                # 3. Fallback path
                fb_dest = aux._FallbackDestination(
                    provider="openai-codex",
                    model="gpt-6-astra-900k",
                    base_url="https://chatgpt.com/backend-api/codex/",
                    api_mode=None,
                )
                fb_kwargs = aux._fallback_request_kwargs(
                    fb_dest,
                    task="compression",
                    messages=[{"role": "user", "content": "fallback compress"}],
                    tools=None,
                    temperature=None,
                    max_tokens=None,
                    effective_timeout=300.0,
                    effective_extra_body={},
                    reasoning_config=None,
                    fallback_entry={"provider": "openai-codex", "model": "gpt-6-astra-900k"},
                    task_config={},
                    apply_fast_lane=False,
                )
                assert fb_kwargs.get("no_progress_timeout") == 180.0
                fb_guard = aux._CodexStreamGuard(
                    mock_aux_client._real_client,
                    total_timeout=300.0,
                    no_progress_timeout=fb_kwargs.get("no_progress_timeout"),
                )
                assert fb_guard.no_progress_timeout == 180.0

    def test_default_timeout_preserves_60s_guard_across_retry_and_fallback(self):
        """When auxiliary.compression.no_progress_timeout is unset,
        no_progress_timeout is None in kwargs and guards default to 60.0s."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            hermes_home = os.path.join(tmp_dir, ".hermes")
            os.makedirs(hermes_home, exist_ok=True)
            config_path = os.path.join(hermes_home, "config.yaml")

            with open(config_path, "w") as f:
                yaml.safe_dump({}, f)

            with patch.dict(os.environ, {"HERMES_HOME": hermes_home}):
                hermes_config._LOAD_CONFIG_CACHE.clear()

                assert aux._get_task_no_progress_timeout("compression") is None

                mock_aux_client = _make_mock_codex_client()

                with patch.object(aux, "_get_cached_client", return_value=(mock_aux_client, "gpt-6-astra-900k")):
                    retry_client, retry_kwargs = aux._prepare_same_provider_retry(
                        task="compression",
                        resolved_provider="openai-codex",
                        resolved_model="gpt-6-astra-900k",
                        resolved_base_url="https://chatgpt.com/backend-api/codex/",
                        resolved_api_key="mock_key",
                        resolved_api_mode=None,
                        main_runtime={"provider": "openai-codex", "model": "gpt-6-astra-900k"},
                        final_model="gpt-6-astra-900k",
                        messages=[{"role": "user", "content": "retry"}],
                        temperature=None,
                        max_tokens=None,
                        tools=None,
                        effective_timeout=300.0,
                        effective_extra_body={},
                        reasoning_config=None,
                        async_mode=False,
                    )
                assert "no_progress_timeout" not in retry_kwargs
                guard = aux._CodexStreamGuard(
                    mock_aux_client._real_client,
                    total_timeout=300.0,
                    no_progress_timeout=retry_kwargs.get("no_progress_timeout"),
                )
                assert guard.no_progress_timeout == 60.0

                fb_dest = aux._FallbackDestination(
                    provider="openai-codex",
                    model="gpt-6-astra-900k",
                    base_url="https://chatgpt.com/backend-api/codex/",
                    api_mode=None,
                )
                fb_kwargs = aux._fallback_request_kwargs(
                    fb_dest,
                    task="compression",
                    messages=[{"role": "user", "content": "fallback"}],
                    tools=None,
                    temperature=None,
                    max_tokens=None,
                    effective_timeout=300.0,
                    effective_extra_body={},
                    reasoning_config=None,
                    fallback_entry={"provider": "openai-codex", "model": "gpt-6-astra-900k"},
                    task_config={},
                    apply_fast_lane=False,
                )
                assert "no_progress_timeout" not in fb_kwargs

    def test_non_codex_provider_never_receives_no_progress_timeout(self):
        """Even with compression no_progress_timeout configured, non-Codex
        destinations (OpenAI, Anthropic, OpenRouter) NEVER receive the kwarg."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            hermes_home = os.path.join(tmp_dir, ".hermes")
            os.makedirs(hermes_home, exist_ok=True)
            config_path = os.path.join(hermes_home, "config.yaml")

            cfg_data = {
                "auxiliary": {
                    "compression": {
                        "no_progress_timeout": 180,
                    }
                }
            }
            with open(config_path, "w") as f:
                yaml.safe_dump(cfg_data, f)

            with patch.dict(os.environ, {"HERMES_HOME": hermes_home}):
                hermes_config._LOAD_CONFIG_CACHE.clear()

                mock_real_openai = MagicMock()
                mock_real_openai.base_url = "https://api.openai.com/v1"

                # Retry on non-codex provider
                with patch.object(aux, "_get_cached_client", return_value=(mock_real_openai, "gpt-4.1")):
                    retry_client, retry_kwargs = aux._prepare_same_provider_retry(
                        task="compression",
                        resolved_provider="openai",
                        resolved_model="gpt-4.1",
                        resolved_base_url="https://api.openai.com/v1",
                        resolved_api_key="mock_key",
                        resolved_api_mode=None,
                        main_runtime={"provider": "openai", "model": "gpt-4.1"},
                        final_model="gpt-4.1",
                        messages=[{"role": "user", "content": "retry"}],
                        temperature=None,
                        max_tokens=None,
                        tools=None,
                        effective_timeout=300.0,
                        effective_extra_body={},
                        reasoning_config=None,
                        async_mode=False,
                    )
                assert "no_progress_timeout" not in retry_kwargs

                # Fallback to non-codex provider
                fb_dest = aux._FallbackDestination(
                    provider="openrouter",
                    model="anthropic/claude-3-haiku",
                    base_url="https://openrouter.ai/api/v1",
                    api_mode=None,
                )
                fb_kwargs = aux._fallback_request_kwargs(
                    fb_dest,
                    task="compression",
                    messages=[{"role": "user", "content": "fallback"}],
                    tools=None,
                    temperature=None,
                    max_tokens=None,
                    effective_timeout=300.0,
                    effective_extra_body={},
                    reasoning_config=None,
                    fallback_entry={"provider": "openrouter", "model": "anthropic/claude-3-haiku"},
                    task_config={},
                    apply_fast_lane=False,
                )
                assert "no_progress_timeout" not in fb_kwargs

    def test_task_scoping_no_leakage_to_other_tasks(self):
        """Compression timeout override (180s) does not leak to another task
        (such as title_generation) which has no override."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            hermes_home = os.path.join(tmp_dir, ".hermes")
            os.makedirs(hermes_home, exist_ok=True)
            config_path = os.path.join(hermes_home, "config.yaml")

            cfg_data = {
                "auxiliary": {
                    "compression": {
                        "no_progress_timeout": 180,
                    },
                    "title_generation": {
                        "timeout": 60,
                    }
                }
            }
            with open(config_path, "w") as f:
                yaml.safe_dump(cfg_data, f)

            with patch.dict(os.environ, {"HERMES_HOME": hermes_home}):
                hermes_config._LOAD_CONFIG_CACHE.clear()

                mock_aux_client = _make_mock_codex_client()

                with patch.object(aux, "_get_cached_client", return_value=(mock_aux_client, "gpt-6-astra-900k")):
                    retry_client, retry_kwargs = aux._prepare_same_provider_retry(
                        task="title_generation",
                        resolved_provider="openai-codex",
                        resolved_model="gpt-6-astra-900k",
                        resolved_base_url="https://chatgpt.com/backend-api/codex/",
                        resolved_api_key="mock_key",
                        resolved_api_mode=None,
                        main_runtime={"provider": "openai-codex", "model": "gpt-6-astra-900k"},
                        final_model="gpt-6-astra-900k",
                        messages=[{"role": "user", "content": "title"}],
                        temperature=None,
                        max_tokens=None,
                        tools=None,
                        effective_timeout=60.0,
                        effective_extra_body={},
                        reasoning_config=None,
                        async_mode=False,
                    )
                assert "no_progress_timeout" not in retry_kwargs

                fb_dest = aux._FallbackDestination(
                    provider="openai-codex",
                    model="gpt-6-astra-900k",
                    base_url="https://chatgpt.com/backend-api/codex/",
                    api_mode=None,
                )
                fb_kwargs = aux._fallback_request_kwargs(
                    fb_dest,
                    task="title_generation",
                    messages=[{"role": "user", "content": "title"}],
                    tools=None,
                    temperature=None,
                    max_tokens=None,
                    effective_timeout=60.0,
                    effective_extra_body={},
                    reasoning_config=None,
                    fallback_entry={"provider": "openai-codex", "model": "gpt-6-astra-900k"},
                    task_config={},
                    apply_fast_lane=False,
                )
                assert "no_progress_timeout" not in fb_kwargs

    @pytest.mark.asyncio
    async def test_async_retry_and_fallback_propagation(self):
        """Async retry and fallback paths correctly propagate no_progress_timeout."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            hermes_home = os.path.join(tmp_dir, ".hermes")
            os.makedirs(hermes_home, exist_ok=True)
            config_path = os.path.join(hermes_home, "config.yaml")

            cfg_data = {
                "auxiliary": {
                    "compression": {
                        "no_progress_timeout": 180,
                    }
                }
            }
            with open(config_path, "w") as f:
                yaml.safe_dump(cfg_data, f)

            with patch.dict(os.environ, {"HERMES_HOME": hermes_home}):
                hermes_config._LOAD_CONFIG_CACHE.clear()

                mock_aux_client = _make_mock_codex_client()
                mock_async_client = aux.AsyncCodexAuxiliaryClient(mock_aux_client)

                # Async retry kwargs preparation
                with patch.object(aux, "_get_cached_client", return_value=(mock_async_client, "gpt-6-astra-900k")):
                    retry_client, retry_kwargs = aux._prepare_same_provider_retry(
                        task="compression",
                        resolved_provider="openai-codex",
                        resolved_model="gpt-6-astra-900k",
                        resolved_base_url="https://chatgpt.com/backend-api/codex/",
                        resolved_api_key="mock_key",
                        resolved_api_mode=None,
                        main_runtime={"provider": "openai-codex", "model": "gpt-6-astra-900k"},
                        final_model="gpt-6-astra-900k",
                        messages=[{"role": "user", "content": "async retry"}],
                        temperature=None,
                        max_tokens=None,
                        tools=None,
                        effective_timeout=300.0,
                        effective_extra_body={},
                        reasoning_config=None,
                        async_mode=True,
                    )
                assert retry_kwargs.get("no_progress_timeout") == 180.0

                # Async fallback candidate kwargs planning
                dest, fb_kwargs, rebuild = aux._plan_fallback_candidate(
                    mock_async_client,
                    "gpt-6-astra-900k",
                    "fallback_chain[0](openai-codex)",
                    task="compression",
                    effective_timeout=300.0,
                    apply_fast_lane=False,
                    messages=[{"role": "user", "content": "async fallback"}],
                    tools=None,
                    temperature=None,
                    max_tokens=None,
                    effective_extra_body={},
                    reasoning_config=None,
                )
                assert fb_kwargs.get("no_progress_timeout") == 180.0

                # Rebuild destination kwargs on auth recovery retry
                rebuild_dest, rebuild_kwargs = rebuild("openai-codex", mock_async_client, "gpt-6-astra-900k")
                assert rebuild_kwargs.get("no_progress_timeout") == 180.0
