"""Regression audit unit testing Codex Responses API usage accounting contract.

Verifies:
1. Total input = cached + uncached without double counting.
2. Output reasoning is a subset of output tokens, not additional output.
3. Zero cache and absent cache fields are handled cleanly.
4. Observed behavior on malformed cache counters: impossible cached > input
   returns prompt_tokens == cached_tokens (upstream total not preserved).
5. No quota-% derivation: token arithmetic remains pure token counts, distinct
   from subscription quota depletion telemetry.
"""

from types import SimpleNamespace

from agent.usage_pricing import CanonicalUsage, normalize_usage


def test_codex_responses_total_input_cached_and_uncached_accounting():
    """Documented Responses fixture proving total input = cached + uncached without double-counting.

    Responses API reports `input_tokens` as the total prompt tokens (inclusive of cached tokens).
    `normalize_usage` splits this into:
      - input_tokens: uncached remainder (prompt_total - cache_read - cache_write)
      - cache_read_tokens: cache hits (input_tokens_details.cached_tokens)
      - cache_write_tokens: cache creation (input_tokens_details.cache_write_tokens / cache_creation_tokens)

    CanonicalUsage.prompt_tokens restores total input = uncached + cached (never double counted).
    """
    # 1. Actual Codex observed fields: only cached_tokens is emitted by the wire Responses API.
    actual_codex_dict = {
        "input_tokens": 1500,
        "output_tokens": 300,
        "input_tokens_details": {
            "cached_tokens": 1000,
        },
        "output_tokens_details": {
            "reasoning_tokens": 80,
        },
    }
    actual_codex_obj = SimpleNamespace(
        input_tokens=1500,
        output_tokens=300,
        input_tokens_details=SimpleNamespace(cached_tokens=1000),
        output_tokens_details=SimpleNamespace(reasoning_tokens=80),
    )

    for payload in (actual_codex_dict, actual_codex_obj):
        normalized = normalize_usage(payload, provider="openai-codex", api_mode="codex_responses")
        assert isinstance(normalized, CanonicalUsage)
        assert normalized.cache_read_tokens == 1000
        assert normalized.cache_write_tokens == 0
        assert normalized.input_tokens == 500
        assert normalized.prompt_tokens == 1500
        assert normalized.prompt_tokens == normalized.input_tokens + normalized.cache_read_tokens

    # 2. Synthetic compatibility fixture: cache_write_tokens is supported by _CODEX_USAGE_SHAPE
    # as an extension/fallback, not an observed wire field from the Codex Responses API.
    synthetic_compat_dict = {
        "input_tokens": 1500,
        "output_tokens": 300,
        "input_tokens_details": {
            "cached_tokens": 1000,
            "cache_write_tokens": 200,
        },
        "output_tokens_details": {
            "reasoning_tokens": 80,
        },
    }
    synthetic_compat_obj = SimpleNamespace(
        input_tokens=1500,
        output_tokens=300,
        input_tokens_details=SimpleNamespace(
            cached_tokens=1000,
            cache_write_tokens=200,
        ),
        output_tokens_details=SimpleNamespace(
            reasoning_tokens=80,
        ),
    )

    for payload in (synthetic_compat_dict, synthetic_compat_obj):
        normalized = normalize_usage(payload, provider="openai-codex", api_mode="codex_responses")
        assert isinstance(normalized, CanonicalUsage)
        assert normalized.cache_read_tokens == 1000
        assert normalized.cache_write_tokens == 200
        assert normalized.input_tokens == 300
        assert normalized.prompt_tokens == 1500
        assert normalized.prompt_tokens == (
            normalized.input_tokens + normalized.cache_read_tokens + normalized.cache_write_tokens
        )


def test_codex_responses_output_reasoning_subset_not_additional():
    """Reasoning tokens are an internal subset of completion/output tokens, not added on top.

    Responses API output_tokens_details.reasoning_tokens must populate CanonicalUsage.reasoning_tokens
    while CanonicalUsage.output_tokens remains the raw output count (not output + reasoning),
    and total_tokens remains prompt_tokens + output_tokens.
    """
    payload = {
        "input_tokens": 500,
        "output_tokens": 200,
        "input_tokens_details": {
            "cached_tokens": 100,
        },
        "output_tokens_details": {
            "reasoning_tokens": 70,
        },
    }

    normalized = normalize_usage(payload, provider="openai-codex", api_mode="codex_responses")

    assert normalized.output_tokens == 200
    assert normalized.reasoning_tokens == 70
    # Reasoning is a subset: total_tokens = prompt (500) + output (200) = 700 (NOT 770)
    assert normalized.total_tokens == 700
    assert normalized.total_tokens == normalized.prompt_tokens + normalized.output_tokens


def test_codex_responses_zero_cache_and_absent_cache_handling():
    """Missing or zeroed cache details should cleanly map all input tokens to uncached input."""
    # Case 1: absent cache details
    absent_cache_payload = {
        "input_tokens": 800,
        "output_tokens": 150,
    }
    norm_absent = normalize_usage(absent_cache_payload, provider="openai-codex", api_mode="codex_responses")
    assert norm_absent.input_tokens == 800
    assert norm_absent.cache_read_tokens == 0
    assert norm_absent.cache_write_tokens == 0
    assert norm_absent.prompt_tokens == 800

    # Case 2: zero cache details
    zero_cache_payload = {
        "input_tokens": 800,
        "output_tokens": 150,
        "input_tokens_details": {
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        },
    }
    norm_zero = normalize_usage(zero_cache_payload, provider="openai-codex", api_mode="codex_responses")
    assert norm_zero.input_tokens == 800
    assert norm_zero.cache_read_tokens == 0
    assert norm_zero.cache_write_tokens == 0
    assert norm_zero.prompt_tokens == 800


def test_codex_responses_malformed_cache_observed_behavior():
    """Observed behavior on malformed cache counters.

    Note: When impossible cached_tokens (350) > input_tokens (200), input_tokens
    is clamped to 0 by max(0, prompt_total - cached), so prompt_tokens resolves to
    0 + 350 = 350. Upstream total (200) is NOT preserved in CanonicalUsage.prompt_tokens.
    This test asserts the current observed behavior without claiming safe-clamping correctness.
    """
    # Case 1: Negative cached counters from malfunctioning proxy/provider clamped to 0
    negative_payload = {
        "input_tokens": 600,
        "output_tokens": 100,
        "input_tokens_details": {
            "cached_tokens": -50,
            "cache_write_tokens": -25,
        },
        "output_tokens_details": {
            "reasoning_tokens": -10,
        },
    }
    norm_neg = normalize_usage(negative_payload, provider="openai-codex", api_mode="codex_responses")
    assert norm_neg.cache_read_tokens == 0
    assert norm_neg.cache_write_tokens == 0
    assert norm_neg.reasoning_tokens == 0
    assert norm_neg.input_tokens == 600
    assert norm_neg.prompt_tokens == 600

    # Case 2: Impossible cache count (cached 350 > input 200).
    # Observed behavior: input_tokens clamps to 0, resulting in prompt_tokens = 350;
    # upstream total of 200 is not preserved.
    impossible_payload = {
        "input_tokens": 200,
        "output_tokens": 50,
        "input_tokens_details": {
            "cached_tokens": 350,
        },
    }
    norm_imp = normalize_usage(impossible_payload, provider="openai-codex", api_mode="codex_responses")
    assert norm_imp.cache_read_tokens == 350
    assert norm_imp.input_tokens == 0
    assert norm_imp.prompt_tokens == 350


def test_codex_responses_no_quota_percent_derivation():
    """Token normalization performs strict token counting without deriving subscription quota %.

    Subscription quota / depletion percentages belong to rate-limit / account telemetry,
    not CanonicalUsage.
    """
    payload = {
        "input_tokens": 250_000,
        "output_tokens": 1_000,
        "input_tokens_details": {
            "cached_tokens": 200_000,
        },
    }
    canonical = normalize_usage(payload, provider="openai-codex", api_mode="codex_responses")

    # CanonicalUsage attributes are purely token and call counts
    assert not hasattr(canonical, "used_percent")
    assert not hasattr(canonical, "quota_percent")
    assert not hasattr(canonical, "depletion_ratio")

    # Pure token arithmetic
    assert canonical.input_tokens == 50_000
    assert canonical.cache_read_tokens == 200_000
    assert canonical.cache_write_tokens == 0
    assert canonical.prompt_tokens == 250_000
