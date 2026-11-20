"""Tests for compaction effective-budget observability and structured budget reporting.

Verifies that ContextCompressor exposes non-mutating structured budget reporting
using the exact same production trigger computation without policy changes.
"""

import json
import logging
from unittest.mock import patch

import pytest

from agent.compaction_budget import CompactionBudgetReport
from agent.context_compressor import ContextCompressor
from agent.model_metadata import MINIMUM_CONTEXT_LENGTH


# ---------------------------------------------------------------------------
# 1. Nominal Context Tests
# ---------------------------------------------------------------------------

class TestCompactionBudgetNominalContext:
    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_nominal_budget_report_fields_and_types(self, _mock_ctx):
        """Verify all required budget report fields, aliases, and attribute access for a nominal 1M context."""
        cc = ContextCompressor(
            model="nominal-model",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=1_000_000,
        )
        report = cc.get_budget_report()

        assert isinstance(report, CompactionBudgetReport)
        assert isinstance(report, dict)

        # Context limit
        assert report["context_limit"] == 1_000_000
        assert report.context_limit == 1_000_000
        assert report["context_length"] == 1_000_000

        # Output reservation
        assert report["output_reservation"] == 0
        assert report.output_reservation == 0
        assert report["max_tokens"] == 0
        assert report["output_reservation_source"] == "none"

        # Usable tokens
        assert report["usable_tokens"] == 1_000_000
        assert report.usable_tokens == 1_000_000
        assert report["usable_input_budget"] == 1_000_000

        # Effective model ratio
        assert report["effective_model_ratio"] == 0.50
        assert report.effective_model_ratio == 0.50
        assert report["effective_threshold_percent"] == 0.50
        assert report["base_threshold_percent"] == 0.50
        assert report["configured_ratio"] == 0.50

        # Requested / effective cap (null cap semantics)
        assert report["requested_cap"] is None
        assert report.requested_cap is None
        assert report["requested_absolute_cap"] is None
        assert report["effective_cap"] is None
        assert report.effective_cap is None
        assert report["effective_threshold_cap"] is None
        assert report["cap_overrides_ratio"] is False

        # Proportional and actual trigger
        assert report["proportional_trigger"] == 500_000
        assert report.proportional_trigger == 500_000
        assert report["actual_trigger"] == 500_000
        assert report.actual_trigger == 500_000
        assert report["threshold_tokens"] == 500_000
        assert report["actual_threshold_tokens"] == 500_000

        # Limiting reason and source
        assert report["limiting_reason"] == "proportional"
        assert report.limiting_reason == "proportional"
        assert report["limiting_source"] == "model_ratio"

        # Metadata context and safety headroom
        assert report["safety_headroom"] == 1024
        assert report["safety_clamp_tokens"] == 1_000_000 - 1024
        assert report["minimum_context_floor"] == MINIMUM_CONTEXT_LENGTH
        assert report["small_context_ceiling"] == int(1_000_000 * 0.85)

        # Dictionary / serialization helper
        as_dict = report.to_dict()
        assert isinstance(as_dict, dict)
        serialized = json.dumps(report)
        assert "500000" in serialized


# ---------------------------------------------------------------------------
# 2. Runtime and Base Context Tests
# ---------------------------------------------------------------------------

class TestCompactionBudgetRuntimeAndBaseContext:
    @patch("agent.context_compressor.get_model_context_length")
    def test_base_context_length_resolved_from_probe(self, mock_ctx):
        """Base context length resolves cleanly from the model probe without explicit config."""
        mock_ctx.return_value = 256_000
        cc = ContextCompressor(model="probed-model", threshold_percent=0.50, quiet_mode=True)
        report = cc.get_budget_report()

        assert report["context_limit"] == 256_000
        mock_ctx.assert_called_once()

    @patch("agent.context_compressor.get_model_context_length")
    def test_runtime_context_length_setter_updates_budget_report(self, mock_ctx):
        """Setting cc.context_length at runtime updates the budget report accordingly."""
        mock_ctx.return_value = 200_000
        cc = ContextCompressor(model="runtime-model", threshold_percent=0.50, quiet_mode=True)

        # 200K window (< 512K) -> floored to 0.75
        initial_report = cc.get_budget_report()
        assert initial_report["context_limit"] == 200_000
        assert initial_report["effective_model_ratio"] == 0.75
        assert initial_report["actual_trigger"] == 150_000
        assert initial_report["actual_trigger"] == cc.threshold_tokens

        # Dynamically widen window to 1M (>= 512K) -> unfloored 0.50
        cc.context_length = 1_000_000
        updated_report = cc.get_budget_report()
        assert updated_report["context_limit"] == 1_000_000
        assert updated_report["effective_model_ratio"] == 0.50
        assert updated_report["actual_trigger"] == 500_000
        assert updated_report["actual_trigger"] == cc.threshold_tokens


# ---------------------------------------------------------------------------
# 3. Model Switch Ratio Tests
# ---------------------------------------------------------------------------

class TestCompactionBudgetModelSwitchRatio:
    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_preview_budget_report_does_not_mutate_compressor(self, _mock_ctx):
        """Previewing a model switch with different ratio returns correct report without mutating state."""
        model_thresholds = {"fast-agent": 0.80, "deliberate-agent": 0.30}
        cc = ContextCompressor(
            model="fast-agent",
            threshold_percent=0.50,
            model_thresholds=model_thresholds,
            config_context_length=1_000_000,
            quiet_mode=True,
        )

        initial_trigger = cc.threshold_tokens
        assert initial_trigger == 800_000
        assert cc.threshold_percent == 0.80

        # Snapshot internal state for nonmutation check
        initial_model = cc.model
        initial_tokens = cc._threshold_tokens
        initial_percent = cc.threshold_percent

        # Preview switch to deliberate-agent at 1M context
        preview = cc.get_budget_report(model="deliberate-agent", context_length=1_000_000)
        assert preview["effective_model_ratio"] == 0.30
        assert preview["base_threshold_percent"] == 0.30
        assert preview["proportional_trigger"] == 300_000
        assert preview["actual_trigger"] == 300_000
        assert preview["actual_trigger"] == cc.preview_threshold_tokens("deliberate-agent", 1_000_000)

        # Verify compressor was not mutated
        assert cc.model == initial_model
        assert cc._threshold_tokens == initial_tokens
        assert cc.threshold_tokens == initial_trigger
        assert cc.threshold_percent == initial_percent

        # Now execute actual update_model and verify live report matches
        cc.update_model("deliberate-agent", 1_000_000)
        live_report = cc.get_budget_report()
        assert live_report["effective_model_ratio"] == 0.30
        assert live_report["actual_trigger"] == 300_000
        assert live_report["actual_trigger"] == cc.threshold_tokens


# ---------------------------------------------------------------------------
# 4. Null and Absent Cap Semantics Tests
# ---------------------------------------------------------------------------

class TestCompactionBudgetNullAndAbsentCap:
    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    @pytest.mark.parametrize("cap_val", [None, 0, -500, "invalid", ""])
    def test_null_and_falsy_caps_treated_as_no_cap(self, _mock_ctx, cap_val):
        """None, zero, negative, or non-numeric caps result in requested_cap=None and effective_cap=None."""
        cc = ContextCompressor(
            model="cap-model",
            threshold_percent=0.50,
            threshold_tokens_cap=cap_val,
            config_context_length=1_000_000,
            quiet_mode=True,
        )
        report = cc.get_budget_report()
        assert report["requested_cap"] is None
        assert report["effective_cap"] is None
        assert report["cap_overrides_ratio"] is False
        assert report["actual_trigger"] == 500_000
        assert report["limiting_reason"] == "proportional"

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_active_cap_overrides_configured_ratio(self, _mock_ctx):
        """When threshold_tokens_cap is smaller than ratio trigger, cap overrides ratio and reports reason."""
        cc = ContextCompressor(
            model="cap-model",
            threshold_percent=0.50,
            threshold_tokens_cap=350_000,
            config_context_length=1_000_000,
            quiet_mode=True,
        )
        report = cc.get_budget_report()
        assert report["requested_cap"] == 350_000
        assert report["effective_cap"] == 350_000
        assert report["proportional_trigger"] == 500_000
        assert report["actual_trigger"] == 350_000
        assert report["cap_overrides_ratio"] is True
        assert report["limiting_reason"] == "effective_cap"
        assert report["limiting_source"] == "threshold_tokens_cap"
        assert report["actual_trigger"] == cc.threshold_tokens

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_cap_larger_than_window_clamped_to_context_limit(self, _mock_ctx):
        """When cap exceeds context window, effective_cap is clamped to context_limit but does not override ratio."""
        cc = ContextCompressor(
            model="cap-model",
            threshold_percent=0.50,
            threshold_tokens_cap=1_500_000,
            config_context_length=1_000_000,
            quiet_mode=True,
        )
        report = cc.get_budget_report()
        assert report["requested_cap"] == 1_500_000
        assert report["effective_cap"] == 1_000_000
        assert report["proportional_trigger"] == 500_000
        assert report["actual_trigger"] == 500_000
        assert report["cap_overrides_ratio"] is False
        assert report["limiting_reason"] == "proportional"


# ---------------------------------------------------------------------------
# 5. Output Override Tests
# ---------------------------------------------------------------------------

class TestCompactionBudgetOutputOverride:
    @patch("agent.model_metadata_endpoint_cache.get_model_max_output_tokens", return_value=65_536)
    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_native_model_metadata_output_reservation(self, _mock_ctx, _mock_output):
        """A model's metadata output ceiling is reserved when max_tokens is unset."""
        cc = ContextCompressor(
            model="gpt-6-astra",
            provider="",
            threshold_percent=0.50,
            max_tokens=None,
            config_context_length=1_000_000,
            quiet_mode=True,
        )
        report = cc.get_budget_report()
        assert report["output_reservation"] == 65_536
        assert report["output_reservation_source"] == "model_metadata"
        assert report["usable_tokens"] == 1_000_000 - 65_536  # 934,464
        expected_trigger = int(934_464 * 0.50)  # 467,232
        assert report["actual_trigger"] == expected_trigger
        assert report["actual_trigger"] == cc.threshold_tokens

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_explicit_max_tokens_overrides_metadata(self, _mock_ctx):
        """Explicit positive max_tokens overrides native metadata ceiling."""
        cc = ContextCompressor(
            model="gpt-6-astra",
            provider="",
            threshold_percent=0.50,
            max_tokens=8_192,
            config_context_length=1_000_000,
            quiet_mode=True,
        )
        report = cc.get_budget_report()
        assert report["output_reservation"] == 8_192
        assert report["output_reservation_source"] == "explicit_max_tokens"
        assert report["usable_tokens"] == 1_000_000 - 8_192  # 991,808
        expected_trigger = int(991_808 * 0.50)  # 495,904
        assert report["actual_trigger"] == expected_trigger
        assert report["actual_trigger"] == cc.threshold_tokens


# ---------------------------------------------------------------------------
# 6. Limit Safety Tests
# ---------------------------------------------------------------------------

class TestCompactionBudgetLimitSafety:
    def test_hard_safety_clamp_preserves_headroom_on_small_usable_budget(self):
        """When usable budget is small, hard safety clamp (1024 tokens headroom) binds and is reported."""
        # context_length = 2000, max_tokens = 500 -> usable_tokens = 1500
        # hard_input_cap = max(1, 1500 - 1024) = 476
        report = ContextCompressor.compute_budget_report(
            context_length=2000,
            threshold_percent=0.75,
            max_tokens=500,
            model="small-window-model",
        )
        assert report["context_limit"] == 2000
        assert report["output_reservation"] == 500
        assert report["usable_tokens"] == 1500
        assert report["safety_headroom"] == 1024
        assert report["safety_clamp_tokens"] == 476
        assert report["actual_trigger"] == 476
        assert report["limiting_reason"] == "safety_clamp"
        assert report["limiting_source"] == "safety_headroom"
        # Verify exact match with _compute_threshold_tokens
        assert report["actual_trigger"] == ContextCompressor._compute_threshold_tokens(2000, 0.75, 500)

    def test_minimum_context_floor_reported_when_binding(self):
        """When proportional trigger is below MINIMUM_CONTEXT_LENGTH (64K) on large window, 64K floor binds."""
        # context_length = 600,000 (>= 512K, no small context floor)
        # threshold_percent = 0.05 -> proportional = 30,000 < 64,000
        report = ContextCompressor.compute_budget_report(
            context_length=600_000,
            threshold_percent=0.05,
            model="large-model",
            apply_ratio_floor=False,
        )
        assert report["proportional_trigger"] == 30_000
        assert report["actual_trigger"] == 64_000
        assert report["limiting_reason"] == "minimum_context_floor"
        assert report["limiting_source"] == "minimum_context_floor"
        assert report["actual_trigger"] == ContextCompressor._compute_threshold_tokens(600_000, 0.05)

    def test_small_context_ceiling_reported_when_64k_floor_exceeds_85_percent(self):
        """On a 70K window, 64K floor exceeds 85% of budget, so 85% trigger cap binds."""
        # usable = 70,000 -> 85% cap = 59,500
        report = ContextCompressor.compute_budget_report(
            context_length=70_000,
            threshold_percent=0.50,
            apply_ratio_floor=False,
        )
        assert report["actual_trigger"] == 59_500
        assert report["limiting_reason"] == "small_context_ceiling"
        assert report["limiting_source"] == "min_context_ratio"
        assert report["actual_trigger"] == ContextCompressor._compute_threshold_tokens(70_000, 0.50)


# ---------------------------------------------------------------------------
# 7. Report Equals Actual Trigger and Nonmutation Tests
# ---------------------------------------------------------------------------

class TestCompactionBudgetEquivalenceAndNonmutation:
    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_report_equals_actual_trigger_across_varied_configurations(self, _mock_ctx):
        """Verify report[actual_trigger] == cc.threshold_tokens across diverse configurations."""
        configs = [
            {"model": "m1", "threshold_percent": 0.50},
            {"model": "m2", "threshold_percent": 0.80, "threshold_tokens_cap": 250_000},
            {"model": "gemini-2.5-pro", "provider": "gemini", "threshold_percent": 0.50},
            {"model": "m4", "threshold_percent": 0.50, "max_tokens": 12_000},
        ]
        for cfg in configs:
            cc = ContextCompressor(config_context_length=1_000_000, quiet_mode=True, **cfg)
            report = cc.get_budget_report()
            assert report["actual_trigger"] == cc.threshold_tokens, f"Failed for config {cfg}"

    @patch("agent.context_compressor.get_model_context_length", return_value=500_000)
    def test_nonmutation_before_and_after_get_budget_report(self, _mock_ctx):
        """Calling get_budget_report() with or without arguments does not mutate instance state."""
        cc = ContextCompressor(model="base-model", threshold_percent=0.60, quiet_mode=True, config_context_length=500_000)
        # Pre-resolve to establish base state
        _ = cc.threshold_tokens

        snapshot = {
            "model": cc.model,
            "provider": cc.provider,
            "context_length": cc.context_length,
            "threshold_tokens": cc.threshold_tokens,
            "threshold_percent": cc.threshold_percent,
            "tail_token_budget": cc.tail_token_budget,
            "max_summary_tokens": cc.max_summary_tokens,
            "max_tokens": cc.max_tokens,
            "threshold_tokens_cap": cc.threshold_tokens_cap,
        }

        # 1. Call without arguments
        _ = cc.get_budget_report()
        for k, v in snapshot.items():
            assert getattr(cc, k) == v, f"Field {k} mutated after empty get_budget_report()"

        # 2. Call with preview arguments
        preview = cc.get_budget_report(
            model="other-model",
            context_length=800_000,
            provider="other-provider",
            max_tokens=4096,
            threshold_tokens_cap=100_000,
        )
        assert preview["context_limit"] == 800_000
        assert preview["actual_trigger"] == 100_000

        for k, v in snapshot.items():
            assert getattr(cc, k) == v, f"Field {k} mutated after preview get_budget_report()"


# ---------------------------------------------------------------------------
# 8. Observability and Diagnostics Logging Tests
# ---------------------------------------------------------------------------

class TestCompactionBudgetObservabilityAndLogs:
    def test_init_log_makes_cap_override_explicit(self, caplog):
        """When cap overrides configured ratio, init log explicitly notes cap override."""
        with caplog.at_level(logging.INFO, logger="agent.context_compressor"):
            cc = ContextCompressor(
                model="cap-log-model",
                threshold_percent=0.50,
                threshold_tokens_cap=200_000,
                config_context_length=1_000_000,
                quiet_mode=False,
            )
            _ = cc.context_length

        init_records = [r for r in caplog.records if "Context compressor initialized:" in r.getMessage()]
        assert len(init_records) == 1
        msg = init_records[0].getMessage()
        assert "cap 200000 overrides configured ratio 50%" in msg

    def test_update_model_log_makes_cap_override_explicit(self, caplog):
        """When update_model encounters cap override, updated model log explicitly notes cap override."""
        cc = ContextCompressor(
            model="initial-model",
            threshold_percent=0.50,
            threshold_tokens_cap=200_000,
            config_context_length=1_000_000,
            quiet_mode=False,
        )
        with caplog.at_level(logging.INFO, logger="agent.context_compressor"):
            cc.update_model("switched-model", 1_000_000)

        update_records = [r for r in caplog.records if "Context compressor model updated:" in r.getMessage()]
        assert len(update_records) == 1
        msg = update_records[0].getMessage()
        assert "cap 200000 overrides configured ratio 50%" in msg

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_telemetry_payload_includes_effective_budget_without_secrets(self, _mock_ctx):
        """Compression attempt telemetry includes structured budget fields and zero secrets."""
        cc = ContextCompressor(
            model="telemetry-model",
            threshold_percent=0.50,
            threshold_tokens_cap=300_000,
            config_context_length=1_000_000,
            quiet_mode=True,
        )
        payload = cc._create_compression_telemetry_payload(current_tokens=400_000)

        assert payload["budget_context_limit"] == 1_000_000
        assert payload["budget_usable_tokens"] == 1_000_000
        assert payload["budget_effective_ratio"] == 0.50
        assert payload["budget_requested_cap"] == 300_000
        assert payload["budget_effective_cap"] == 300_000
        assert payload["budget_proportional_trigger"] == 500_000
        assert payload["budget_actual_trigger"] == 300_000
        assert payload["budget_limiting_reason"] == "effective_cap"
        assert payload["budget_limiting_source"] == "threshold_tokens_cap"
        assert payload["budget_cap_overrides_ratio"] is True

        serialized = json.dumps(payload)
        # Ensure no prompt or secrets exist
        assert "content" not in payload
        assert "secret" not in serialized.lower()


# ---------------------------------------------------------------------------
# 9. Model-Family Absolute-Cap Exemption Tests (model families)
# ---------------------------------------------------------------------------

class TestCompactionBudgetModelExemptions:
    """Validate user-authorized absolute-cap exemptions for configured model families."""

    def test_default_no_exemption_preserves_upstream_behavior(self):
        """When no exemptions are configured (default), cap clamps all models including Astra and Claude."""
        # 1M window, 50% ratio = 500,000; 256K cap clamps both to 256,000
        for model, provider in [("gpt-6-astra", "openai-codex"), ("claude-sonnet-4-6", "anthropic")]:
            report = ContextCompressor.compute_budget_report(
                context_length=1_000_000,
                threshold_percent=0.50,
                model=model,
                provider=provider,
                threshold_tokens_cap=256_000,
                threshold_tokens_exempt_models=[],
            )
            assert report["cap_exempt"] is False
            assert report["exemption_reason"] is None
            assert report["config_source"] is None
            assert report["effective_cap"] == 256_000
            assert report["actual_trigger"] == 256_000
            assert report["cap_overrides_ratio"] is True
            assert report["limiting_reason"] == "effective_cap"
            assert report["limiting_source"] == "threshold_tokens_cap"

    def test_astra_canonical_and_aliases_exempt(self):
        """Astra canonical, -900k variants, provider-prefixed routes, and bare 'astra' are exempt."""
        astra_variants = [
            ("gpt-6-astra", "openai-codex"),
            ("gpt-6-astra-900k", "openai-codex"),
            ("openai-codex:gpt-6-astra", "openai-codex"),
            ("openai-codex:gpt-6-astra-900k", "openai-codex"),
            ("openrouter/openai/gpt-6-astra", "openrouter"),
            ("openrouter/openai/gpt-6-astra-900k", "openrouter"),
            ("openai/gpt-6-astra", "openai"),
            ("astra", ""),
            ("astra-900k", ""),
        ]
        exempt_config = ["astra"]
        for model, provider in astra_variants:
            report = ContextCompressor.compute_budget_report(
                context_length=1_000_000,
                threshold_percent=0.90,
                model=model,
                provider=provider,
                threshold_tokens_cap=256_000,
                threshold_tokens_exempt_models=exempt_config,
            )
            assert report["cap_exempt"] is True, f"Failed cap_exempt for {model}"
            assert report["exemption_reason"] == "model_family_exemption"
            assert report["config_source"] == "threshold_tokens_exempt_models"
            assert report["nominal_effective_cap"] == 256_000
            assert report["effective_cap"] is None
            assert report["cap_overrides_ratio"] is False
            # Output reservation applies: 128K for openai-codex astra (usable 872K -> 784,800),
            # 0 for bare astra (usable 1M -> 900,000). Both > 256,000 (cap bypassed, output bound preserved).
            expected_trigger = int(report["usable_tokens"] * 0.90)
            assert report["actual_trigger"] == expected_trigger
            assert report["actual_trigger"] > 256_000



    def test_other_nonexempt_models_cap_persists(self):
        """Other non-exempt models retain absolute cap of 256K."""
        nonexempt = [
            ("gpt-5.6-sol", "openai-codex"),
            ("gpt-4o", "openai"),
            ("deepseek-r1", "deepseek"),
            ("qwen-2.5-72b", "openrouter"),
        ]
        exempt_config = ["astra"]
        for model, provider in nonexempt:
            report = ContextCompressor.compute_budget_report(
                context_length=1_000_000,
                threshold_percent=0.50,
                model=model,
                provider=provider,
                threshold_tokens_cap=256_000,
                threshold_tokens_exempt_models=exempt_config,
            )
            assert report["cap_exempt"] is False
            assert report["effective_cap"] == 256_000
            assert report["actual_trigger"] == 256_000
            assert report["cap_overrides_ratio"] is True
            assert report["limiting_reason"] == "effective_cap"

    def test_safety_bounds_still_apply_to_exempt_models(self):
        """Safety headroom (1024 tokens) and output reservation still bound exempt models."""
        # Usable input: 10,000 - 9,500 max_tokens = 500
        # Hard input cap = max(1, 500 - 1024) = 1
        report = ContextCompressor.compute_budget_report(
            context_length=10_000,
            threshold_percent=0.90,
            max_tokens=9_500,
            model="gpt-6-astra",
            provider="openai-codex",
            threshold_tokens_cap=256_000,
            threshold_tokens_exempt_models=["astra"],
        )
        assert report["cap_exempt"] is True
        assert report["output_reservation"] == 9_500
        assert report["usable_tokens"] == 500
        assert report["actual_trigger"] == 1  # Clamped by safety headroom
        assert report["limiting_reason"] == "safety_clamp"
        assert report["limiting_source"] == "safety_headroom"

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_same_resolution_in_trigger_preview_switch_and_hotreload(self, _mock_ctx):
        """Actual trigger, preview/report, switch, and hot-reload resolve identically."""
        # 1. Actual trigger matches preview report for exempt model
        cc = ContextCompressor(
            model="gpt-6-astra",
            threshold_percent=0.90,
            threshold_tokens_cap=256_000,
            threshold_tokens_exempt_models=["astra"],
            config_context_length=1_000_000,
            quiet_mode=True,
        )
        assert cc.threshold_tokens == 900_000
        report = cc.get_budget_report()
        assert report["actual_trigger"] == 900_000
        assert cc.preview_threshold_tokens("gpt-6-astra", 1_000_000) == 900_000

        # 2. Switch from exempt (Astra) to non-exempt (Claude) clamps trigger to cap
        cc.update_model("claude-sonnet-4-6", 1_000_000, provider="anthropic")
        assert cc.threshold_tokens == 256_000
        switch_report = cc.get_budget_report()
        assert switch_report["cap_exempt"] is False
        assert switch_report["actual_trigger"] == 256_000

        # 3. Switching back to the configured Astra family removes the cap.
        cc.update_model("gpt-6-astra", 1_000_000, provider="")
        assert cc.threshold_tokens == 900_000
        assert cc.get_budget_report()["cap_exempt"] is True

        # 4. Live config hot-reload: removing exemption re-clamps to 256K
        from tui_gateway.session_compression import _apply_live_compression_config
        from types import SimpleNamespace
        dummy_agent = SimpleNamespace(
            context_compressor=cc,
            model="gpt-6-astra",
            provider="",
            base_url="",
            api_key="",
            api_mode="",
            quiet_mode=True,
        )
        # Hot-reload with empty exemptions (threshold explicitly 0.90)
        _apply_live_compression_config(
            dummy_agent,
            {"compression": {"threshold": 0.90, "threshold_tokens": 256_000, "threshold_tokens_exempt_models": []}}
        )
        assert cc.threshold_tokens == 256_000
        hot_report = cc.get_budget_report()
        assert hot_report["cap_exempt"] is False
        assert hot_report["actual_trigger"] == 256_000

        # Hot-reload re-enabling exemptions uncaps again
        _apply_live_compression_config(
            dummy_agent,
            {"compression": {"threshold": 0.90, "threshold_tokens": 256_000, "threshold_tokens_exempt_models": ["astra"]}}
        )
        assert cc.threshold_tokens == 900_000
        hot_report2 = cc.get_budget_report()
        assert hot_report2["cap_exempt"] is True
        assert hot_report2["actual_trigger"] == 900_000

    def test_ab_runtime_active_policy_pipeline(self, tmp_path, monkeypatch):
        """A/B runtime test exercising full config loader -> agent init -> runtime -> budget report.

        Condition A (active policy with Astra exemption):
          - Astra900k: exempt, trigger is un-capped at 694,800
          - Claude: non-exempt, trigger clamped to 256,000 absolute cap

        Condition B (Exemptions disabled: threshold_tokens_exempt_models: []):
          - Astra900k: clamped to 256,000
          - Gemini3.8FlashHigh: clamped to 256,000
          - Claude via GeminiOAuth: clamped to 256,000
        """
        import os
        from types import SimpleNamespace
        from hermes_cli import config as config_mod
        from agent.agent_init import _parse_compression_config, _build_context_engine

        (tmp_path / ".env").touch()

        # Condition A: Active policy values
        active_yaml = """
compression:
  enabled: true
  threshold: 0.5
  threshold_tokens: 256000
  target_ratio: 0.2
  protect_last_n: 20
  protect_first_n: 3
  model_thresholds:
    gpt-6-astra: 0.9
  threshold_tokens_exempt_models:
    - astra
"""
        (tmp_path / "config.yaml").write_text(active_yaml, encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config_mod._RAW_CONFIG_CACHE.clear()

        cfg_a = config_mod.load_config()
        assert cfg_a["compression"]["threshold_tokens_exempt_models"] == ["astra"]

        models = [
            ("Astra900k", "gpt-6-astra-900k", "openai-codex", 900_000),
            ("Claude", "claude-sonnet-4-6", "anthropic", 1_000_000),
        ]

        reports_a = {}
        for label, model, provider, ctx_len in models:
            agent = SimpleNamespace(
                model=model, provider=provider, api_mode="chat_completions",
                quiet_mode=True, base_url="", api_key="", max_tokens=None
            )
            cs = _parse_compression_config(agent, cfg_a)
            _build_context_engine(agent, cfg_a, cs, None, ctx_len, None)
            rep = agent.context_compressor.get_budget_report()
            reports_a[label] = rep

        # Assert Condition A outcomes
        # 1. Astra 900k is exempt
        assert reports_a["Astra900k"]["cap_exempt"] is True
        assert reports_a["Astra900k"]["effective_cap"] is None
        assert reports_a["Astra900k"]["actual_trigger"] == 694_800
        assert reports_a["Astra900k"]["limiting_reason"] == "proportional"

        # 2. Claude is not exempt and remains clamped to 256K cap
        assert reports_a["Claude"]["cap_exempt"] is False
        assert reports_a["Claude"]["effective_cap"] == 256_000
        assert reports_a["Claude"]["actual_trigger"] == 256_000
        assert reports_a["Claude"]["limiting_reason"] == "effective_cap"

        # Condition B: Exemptions disabled
        (tmp_path / "config.yaml").write_text("""
compression:
  enabled: true
  threshold: 0.5
  threshold_tokens: 256000
  target_ratio: 0.2
  protect_last_n: 20
  protect_first_n: 3
  model_thresholds:
    gpt-6-astra: 0.9
  threshold_tokens_exempt_models: []
""", encoding="utf-8")
        config_mod._RAW_CONFIG_CACHE.clear()
        cfg_b = config_mod.load_config()
        assert cfg_b["compression"]["threshold_tokens_exempt_models"] == []

        for label, model, provider, ctx_len in models:
            agent = SimpleNamespace(
                model=model, provider=provider, api_mode="chat_completions",
                quiet_mode=True, base_url="", api_key="", max_tokens=None
            )
            cs = _parse_compression_config(agent, cfg_b)
            _build_context_engine(agent, cfg_b, cs, None, ctx_len, None)
            rep = agent.context_compressor.get_budget_report()
            assert rep["cap_exempt"] is False
            assert rep["effective_cap"] == 256_000
            assert rep["actual_trigger"] == 256_000
            assert rep["limiting_reason"] == "effective_cap"


def test_cached_endpoint_limit_drives_report_and_live_trigger(monkeypatch):
    import time
    from agent import model_metadata

    url = "http://127.0.0.1:12345/v1"
    model = "synthetic-model"
    key = model_metadata._endpoint_memo_key(url, "synthetic-key")
    payload = {"data": [{"id": model, "context_window": 1_000_000, "max_output_tokens": 64_000}]}
    monkeypatch.setitem(model_metadata._endpoint_model_metadata_cache, key, model_metadata._parse_models_payload(payload))
    monkeypatch.setitem(model_metadata._endpoint_model_metadata_cache_time, key, time.time())
    cc = ContextCompressor(model=model, provider="custom", base_url=url, api_key="synthetic-key",
                           config_context_length=1_000_000, threshold_percent=0.5, quiet_mode=True)
    report = cc.get_budget_report()
    assert report["output_reservation"] == 64_000
    assert report["output_reservation_source"] == "endpoint_metadata"
    assert report["actual_trigger"] == cc.threshold_tokens

    explicit = cc.get_budget_report(max_tokens=4_096)
    assert explicit["output_reservation"] == 4_096
    assert explicit["output_reservation_source"] == "explicit_max_tokens"
    other_key = ContextCompressor(model=model, provider="custom", base_url=url, api_key="other-key",
                                  config_context_length=1_000_000, quiet_mode=True)
    assert other_key.get_budget_report()["output_reservation"] == 0
    monkeypatch.setitem(model_metadata._endpoint_model_metadata_cache_time, key,
                        time.time() - model_metadata._ENDPOINT_MODEL_CACHE_TTL - 1)
    assert cc.get_budget_report()["output_reservation"] == 64_000
    stale = ContextCompressor(model=model, provider="custom", base_url=url, api_key="synthetic-key",
                              config_context_length=1_000_000, quiet_mode=True)
    assert stale.get_budget_report()["output_reservation"] == 0
    second_key = model_metadata._endpoint_memo_key(url, "other-key")
    second_payload = {"data": [{"id": model, "context_window": 1_000_000, "max_output_tokens": 8_000}]}
    monkeypatch.setitem(model_metadata._endpoint_model_metadata_cache, second_key,
                        model_metadata._parse_models_payload(second_payload))
    monkeypatch.setitem(model_metadata._endpoint_model_metadata_cache_time, second_key, time.time())
    cc.update_model(model, 1_000_000, base_url=url, api_key="other-key", provider="custom")
    assert cc.get_budget_report()["output_reservation"] == 8_000
    assert cc.get_budget_report()["actual_trigger"] == cc.threshold_tokens


def test_compute_threshold_tokens_astra_and_unknown_models_safe():
    """Model switch to gpt-6-astra or models with unspecified output reservation
    must compute threshold_tokens without TypeError or NoneType subtraction."""
    from agent.context_compressor import ContextCompressor

    # gpt-6-astra on openai has 128K output reservation from builtin metadata
    t_astra = ContextCompressor._compute_threshold_tokens(
        context_length=1_050_000,
        threshold_percent=0.50,
        max_tokens=None,
        model="gpt-6-astra",
        provider="openai",
    )
    assert t_astra == (1_050_000 - 128_000) * 0.50 == 461_000

    # gpt-6-astra-900k variant on codex preserves 128K output reservation
    t_astra_900k = ContextCompressor._compute_threshold_tokens(
        context_length=900_000,
        threshold_percent=0.90,
        max_tokens=None,
        model="gpt-6-astra-900k",
        provider="openai-codex",
    )
    assert t_astra_900k == int((900_000 - 128_000) * 0.90) == 694_800

    # Unspecified/bare model falls back to 0 output reservation and does not raise
    t_bare = ContextCompressor._compute_threshold_tokens(
        context_length=128_000,
        threshold_percent=0.50,
        max_tokens=None,
        model="custom-unknown-model",
        provider="custom",
    )
    assert t_bare == 64_000
