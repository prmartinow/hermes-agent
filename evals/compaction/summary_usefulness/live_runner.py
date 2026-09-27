"""Reusable bounded live evaluation runner for compaction summary usefulness.

Executes one logical production summary-generation request using the configured
auxiliary model and established client credentials, strictly bounded to a synthetic
fixture (aiming for 4k-8k input tokens, max 1500 output).

Note on call semantics:
This runner dispatches one logical call via `call_llm`. Because `call_llm` manages
transient retries and fallbacks internally, transport blips may trigger same-provider
retries under the hood rather than guaranteeing an exact single wire request.

Saves raw output and sanitized metadata to:
<scratch_dir>/compaction-live-quality/

Guarantees:
- Zero production/config modifications.
- Zero credential exposure / config printing (endpoints and keys are completely omitted).
- Strictly synthetic data (no session transcripts or user private data).
- Enforces fail-closed refusal when input exceeds 8,000 tokens.
- Enforces max_tokens (1,500 by default) passed directly to call_llm.
- Uses dynamic scratch directory via TMPDIR runtime or canonical get_hermes_home().
- Uses normal established auth client (GeminiCloudCodeClient / Google PA gateway).
- Uses the actual production prompt template via ContextCompressor._build_summary_prompt.
- Evaluates against deterministic ground-truth rubric and produces detailed factual/semantic report.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.agent_runtime_helpers import strip_think_blocks
from agent.auxiliary_client import (
    call_llm,
    _get_auxiliary_task_config,
    _resolve_call_client,
    _resolve_task_provider_model,
)
from agent.context_compressor import (
    ContextCompressor,
    _redact_compaction_text,
    _reinject_pruned_skill_markers,
)
from evals.compaction.summary_usefulness.fixtures import (
    MIGRATION_CASE_GROUND_TRUTH,
    SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
)
from evals.compaction.summary_usefulness.rubric import (
    SummaryUsefulnessRubric,
    SummaryUsefulnessScore,
)

MAX_INPUT_TOKENS = 8000
DEFAULT_MAX_OUTPUT_TOKENS = 1500


def get_default_scratch_dir() -> Path:
    """Resolve dynamic scratch directory via TMPDIR runtime or canonical get_hermes_home."""
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        return Path(tmpdir) / "compaction-live-quality"
    try:
        from hermes_constants import get_scratch_dir
        return get_scratch_dir() / "compaction-live-quality"
    except Exception:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "cache" / "scratch" / "compaction-live-quality"


DEFAULT_SCRATCH_DIR = get_default_scratch_dir()


def resolve_actual_configured_auxiliary() -> Dict[str, Any]:
    """Read actual configured compression auxiliary model and provider through the normal API.

    Returns sanitized metadata only — never prints or returns credentials, secrets, or endpoints.
    Endpoints are completely omitted rather than relying on bespoke scrubbing.
    """
    cfg = _get_auxiliary_task_config("compression")
    res = _resolve_task_provider_model("compression")
    provider, model, base_url, api_key, api_mode = res

    route = _resolve_call_client(
        "compression",
        provider=None,
        model=None,
        base_url=None,
        api_key=None,
        resolved_provider=provider,
        resolved_model=model,
        resolved_base_url=base_url,
        resolved_api_key=api_key,
        resolved_api_mode=api_mode,
        main_runtime=None,
        async_mode=False,
    )

    client_class = type(route.client).__name__

    return {
        "configured_provider": cfg.get("provider", "auto"),
        "configured_model": cfg.get("model") or "(default)",
        "configured_timeout": cfg.get("timeout", 120),
        "resolved_provider": route.resolved_provider,
        "effective_provider": route.effective_provider,
        "resolved_model": route.final_model,
        "client_class": client_class,
        "auth_type": "Google OAuth (Antigravity Pool)" if "Gemini" in client_class else "API Client",
    }


def build_production_prompt_and_metadata(
    transcript: List[Dict[str, Any]],
    model_name: str,
    provider_name: str,
) -> Tuple[str, Dict[str, Any]]:
    """Construct the exact production summary prompt using ContextCompressor._build_summary_prompt."""
    compressor = ContextCompressor(model=model_name, provider=provider_name, tail_mode="lean")
    turns = [m for m in transcript if m.get("role") != "system"]

    prompt_started_at = time.monotonic()
    records = compressor._serialize_records_for_summary(turns)
    content_to_summarize, coverage = compressor._sample_summary_records(records)
    summary_budget = compressor._compute_summary_budget(turns)
    has_user_turn = compressor._transcript_has_real_user_turn(turns)

    prompt = compressor._build_summary_prompt(
        content_to_summarize=content_to_summarize,
        summary_budget=summary_budget,
        focus_topic=None,
        memory_context="",
        has_user_turn=has_user_turn,
    )

    metadata = {
        "prompt_chars": len(prompt),
        "input_turns_count": len(turns),
        "coverage": coverage,
        "summary_budget": summary_budget,
        "has_user_turn": has_user_turn,
        "prompt_build_ms": int((time.monotonic() - prompt_started_at) * 1000),
    }
    return prompt, metadata


def execute_live_compaction_request(
    transcript: Optional[List[Dict[str, Any]]] = None,
    ground_truth: Optional[Dict[str, Any]] = None,
    scratch_dir: Optional[Path] = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> Dict[str, Any]:
    """Execute one logical production summary generation request on synthetic fixture."""
    if transcript is None:
        transcript = SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED
    if ground_truth is None:
        ground_truth = MIGRATION_CASE_GROUND_TRUTH
    if scratch_dir is None:
        scratch_dir = get_default_scratch_dir()

    scratch_dir.mkdir(parents=True, exist_ok=True)

    # 1. Resolve configured auxiliary route
    aux_info = resolve_actual_configured_auxiliary()
    resolved_model = aux_info["resolved_model"]
    effective_provider = aux_info["effective_provider"]

    if not resolved_model:
        raise RuntimeError(f"No configured model resolved for compression route: {aux_info}")

    # 2. Build exact production prompt
    prompt, prompt_meta = build_production_prompt_and_metadata(
        transcript, resolved_model, effective_provider
    )

    # 3. Get exact input token count via client
    route = _resolve_call_client(
        "compression",
        provider=None,
        model=None,
        base_url=None,
        api_key=None,
        resolved_provider=aux_info["resolved_provider"],
        resolved_model=resolved_model,
        resolved_base_url=None,
        resolved_api_key=None,
        resolved_api_mode=None,
        main_runtime=None,
        async_mode=False,
    )
    exact_input_tokens = route.client.count_tokens(contents=prompt, model=resolved_model)
    prompt_meta["exact_input_tokens"] = exact_input_tokens

    # Verify input token bounds: fail closed if input tokens exceed limit
    if exact_input_tokens > MAX_INPUT_TOKENS:
        raise ValueError(
            f"Input token count {exact_input_tokens} exceeds maximum allowed limit of {MAX_INPUT_TOKENS} tokens (fail closed)."
        )
    if exact_input_tokens < 3500:
        print(f"WARNING: Exact input tokens {exact_input_tokens} below standard target window.")

    # 4. Dispatch one logical request (note: call_llm manages transient retries internally)
    call_route_info: Dict[str, str] = {}
    call_start = time.monotonic()

    response = call_llm(
        task="compression",
        main_runtime={"model": resolved_model, "provider": effective_provider},
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_output_tokens,
        route_info=call_route_info,
    )
    elapsed_seconds = time.monotonic() - call_start

    # 5. Extract raw output and usage
    choices = getattr(response, "choices", [])
    if not choices:
        raise RuntimeError("Live LLM returned empty choices list.")

    choice = choices[0]
    msg = getattr(choice, "message", None)
    raw_content = getattr(msg, "content", "") if msg else ""
    if not raw_content:
        raise RuntimeError("Live LLM returned empty content string.")

    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
    total_tokens = getattr(usage, "total_tokens", None) if usage else None
    cached_tokens = None
    if usage and hasattr(usage, "prompt_tokens_details"):
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = getattr(details, "cached_tokens", None) if details else None

    finish_reason = getattr(choice, "finish_reason", "unknown")

    # 6. Apply standard production cleaning (think blocks strip, redact, grounding)
    cleaned_content = strip_think_blocks(None, raw_content).strip() or raw_content
    cleaned_content = _redact_compaction_text(cleaned_content)
    compressor = ContextCompressor(model=resolved_model, provider=effective_provider, tail_mode="lean")
    turns = [m for m in transcript if m.get("role") != "system"]
    cleaned_summary = compressor._ground_historical_task_snapshot(cleaned_content, turns)
    cleaned_summary = compressor._augment_summary_lean(cleaned_summary, turns)
    final_production_summary = compressor._with_summary_prefix(cleaned_summary)

    # 7. Evaluate against deterministic rubric
    rubric = SummaryUsefulnessRubric(ground_truth)
    raw_transcript_text = "\n".join(f"[{m.get('role')}]: {m.get('content')}" for m in transcript)
    eval_score: SummaryUsefulnessScore = rubric.evaluate(final_production_summary, raw_transcript_text)

    # 8. Save artifacts to exclusive scratch directory
    raw_file = scratch_dir / "raw_summary_output.md"
    raw_file.write_text(raw_content, encoding="utf-8")

    cleaned_file = scratch_dir / "cleaned_summary.md"
    cleaned_file.write_text(final_production_summary, encoding="utf-8")

    gt_file = scratch_dir / "ground_truth.json"
    gt_file.write_text(json.dumps(ground_truth, indent=2), encoding="utf-8")

    metadata = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": resolved_model,
        "provider": effective_provider,
        "client_class": aux_info["client_class"],
        "auth_type": aux_info["auth_type"],
        "finish_reason": finish_reason,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "usage": {
            "exact_prompt_tokens": exact_input_tokens,
            "provider_prompt_tokens": prompt_tokens,
            "provider_completion_tokens": completion_tokens,
            "provider_total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
        },
        "max_output_tokens_configured": max_output_tokens,
        "prompt_metadata": prompt_meta,
        "output_chars": len(raw_content),
        "cleaned_output_chars": len(final_production_summary),
        "artifact_paths": {
            "raw_output": str(raw_file),
            "cleaned_output": str(cleaned_file),
            "ground_truth": str(gt_file),
        },
    }

    metadata_file = scratch_dir / "sanitized_metadata.json"
    metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    # Serialize evaluation score
    score_dict = asdict(eval_score)
    score_file = scratch_dir / "evaluation_score.json"
    score_file.write_text(json.dumps(score_dict, indent=2, default=str), encoding="utf-8")

    return {
        "metadata": metadata,
        "eval_score": eval_score,
        "raw_content": raw_content,
        "cleaned_summary": final_production_summary,
        "artifact_dir": str(scratch_dir),
    }


if __name__ == "__main__":
    print("=== Hermes Bounded Live Compaction Evaluation Runner ===")
    print("Resolving configured compression auxiliary route...")
    aux = resolve_actual_configured_auxiliary()
    print(f"  • Model:            {aux['resolved_model']}")
    print(f"  • Provider:         {aux['effective_provider']}")
    print(f"  • Client Class:     {aux['client_class']}")
    print(f"  • Auth Gateway:     {aux['auth_type']}")

    print("\nExecuting logical production summary request (call_llm manages transient retries internally)...")
    res = execute_live_compaction_request()

    meta = res["metadata"]
    score = res["eval_score"]
    usage = meta["usage"]

    print("\n=== Live Request Execution Completed ===")
    print(f"  • Elapsed:          {meta['elapsed_seconds']:.2f}s")
    print(f"  • Input Tokens:     {usage['exact_prompt_tokens']} (PA exact) / {usage['provider_prompt_tokens']} (reported)")
    print(f"  • Output Tokens:    {usage['provider_completion_tokens']}")
    print(f"  • Total Tokens:     {usage['provider_total_tokens']}")
    print(f"  • Cached Tokens:    {usage.get('cached_tokens', 0)}")
    print(f"  • Raw Output Chars: {meta['output_chars']}")

    print("\n=== Heuristic Proxy Rubric Evaluation ===")
    print(f"  • Usefulness Score: {score.usefulness_score:.4f} / 1.0000")
    print(f"  • Exact Fact Ret.:  {score.matched_required_facts}/{score.total_required_facts} ({score.exact_retention_score*100:.1f}%)")
    print(f"  • Contradictions:   {score.contradiction_count} (active assertions of superseded facts)")
    print(f"  • Review Required:  {score.review_required_count} (historical/ambiguous superseded mentions)")
    print(f"  • Unfinished Intent:{'YES' if score.unfinished_intent_retained else 'NO'}")
    print(f"  • Key Decisions:    {score.decisions_retained}/{score.decisions_total}")
    print(f"  • Failed Approaches:{score.failed_approaches_retained}/{score.failed_approaches_total}")
    print(f"  • Shrink Ratio:     {score.shrink_ratio:.4f}")
    print(f"\nArtifacts saved in: {res['artifact_dir']}")
