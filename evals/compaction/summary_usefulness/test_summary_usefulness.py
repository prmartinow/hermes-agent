"""Tests for summary usefulness evaluation harness, rubric, and production growth guard.

Validates that:
1. Scorer discriminates between good and bad summaries on proxy metrics.
2. Contradictions penalize ONLY explicit synthetic assertions directing active usage.
3. Historical references (e.g. '[terminal] ran old-host...') are classified as review_required,
   NOT active contradictions, leaving semantic contradiction verdicts manual/LLM unknown.
4. Old quoted tool log vs explicit current wrong host vs correct negation are properly distinguished.
5. Rubric makes no heuristic claim to general semantic correctness.
6. Shrink ratio is evaluated independently of fact retention and proxy usefulness.
7. Production deterministic fallback builder runs offline and reports honest retention.
8. Actual production growth guard is executed offline on synthetic builder output,
   demonstrating rejected_would_grow=True and original transcript preserved (runtime never expands).
"""

from __future__ import annotations

import pytest

from evals.compaction.summary_usefulness.fixtures import (
    MIGRATION_CASE_GROUND_TRUTH,
    SYNTHETIC_HANDCRAFTED_BAD_CONTRADICTORY_SUMMARY,
    SYNTHETIC_HANDCRAFTED_BAD_VAGUE_SUMMARY,
    SYNTHETIC_HANDCRAFTED_GOOD_SUMMARY,
    SYNTHETIC_MIGRATION_TRANSCRIPT,
)
from evals.compaction.summary_usefulness.harness import (
    evaluate_summary,
    execute_production_growth_guard_offline,
    run_production_fallback_offline,
    run_raw_fallback_builder_offline,
)


def test_scorer_discrimination():
    """Good summary must score significantly higher on heuristic proxy metrics than bad summaries."""
    gt = MIGRATION_CASE_GROUND_TRUTH
    transcript = SYNTHETIC_MIGRATION_TRANSCRIPT

    score_good = evaluate_summary(SYNTHETIC_HANDCRAFTED_GOOD_SUMMARY, gt, transcript)
    score_contradictory = evaluate_summary(SYNTHETIC_HANDCRAFTED_BAD_CONTRADICTORY_SUMMARY, gt, transcript)
    score_vague = evaluate_summary(SYNTHETIC_HANDCRAFTED_BAD_VAGUE_SUMMARY, gt, transcript)

    # Good summary should have high proxy usefulness
    assert score_good.usefulness_score >= 0.90, f"Expected good score >= 0.90, got {score_good.usefulness_score}"
    assert score_good.exact_retention_score == 1.0
    assert score_good.contradiction_count == 0
    assert score_good.review_required_count == 0
    assert score_good.unfinished_intent_retained is True

    # Bad contradictory summary should be penalized heavily for active assertions of wrong hosts
    assert score_contradictory.usefulness_score <= 0.25, f"Expected contradictory score <= 0.25, got {score_contradictory.usefulness_score}"
    assert score_contradictory.contradiction_count >= 2
    assert score_contradictory.unfinished_intent_retained is False

    # Bad vague summary should have near-zero usefulness despite high shrinkage
    assert score_vague.usefulness_score <= 0.20, f"Expected vague score <= 0.20, got {score_vague.usefulness_score}"
    assert score_vague.exact_retention_score == 0.0

    # Strong discrimination gap
    assert (score_good.usefulness_score - score_contradictory.usefulness_score) >= 0.65
    assert (score_good.usefulness_score - score_vague.usefulness_score) >= 0.70


def test_contradiction_penalty_for_explicit_assertions():
    """Explicitly asserting superseded facts (like legacy host:port) as active instructions must trigger penalties."""
    gt = MIGRATION_CASE_GROUND_TRUTH
    score = evaluate_summary(SYNTHETIC_HANDCRAFTED_BAD_CONTRADICTORY_SUMMARY, gt)

    active_contradictions = [c for c in score.contradiction_results if c.asserted_as_active]
    assert len(active_contradictions) >= 2
    assert any("5432" in c.superseded_fact for c in active_contradictions)
    assert any("pg-legacy-01.internal" in c.superseded_fact for c in active_contradictions)
    assert score.contradiction_penalty > 0.0


def test_shrink_ratio_independence():
    """High token shrinkage must not artificially elevate a useless summary."""
    gt = MIGRATION_CASE_GROUND_TRUTH
    transcript = SYNTHETIC_MIGRATION_TRANSCRIPT

    score_vague = evaluate_summary(SYNTHETIC_HANDCRAFTED_BAD_VAGUE_SUMMARY, gt, transcript)

    # High token shrinkage (small ratio)
    assert score_vague.shrink_ratio < 0.30
    # But proxy usefulness is not inflated by shrinkage
    assert score_vague.usefulness_score == 0.0
    assert score_vague.exact_retention_score == 0.0


def test_raw_fallback_builder_offline_honesty():
    """Verify that ContextCompressor raw fallback builder runs offline and reports retention honestly."""
    transcript = SYNTHETIC_MIGRATION_TRANSCRIPT
    gt = MIGRATION_CASE_GROUND_TRUTH

    fallback_summary = run_raw_fallback_builder_offline(transcript)
    assert isinstance(fallback_summary, str)
    assert len(fallback_summary) > 0

    score = evaluate_summary(fallback_summary, gt, transcript)

    # 1. Fallback captures the latest user turn into historical task snapshot
    assert score.unfinished_intent_retained is True
    # 2. Key decisions are explicitly unrecoverable in static fallback
    assert "None recoverable from deterministic fallback" in fallback_summary
    # 3. Active state is unknown in static fallback
    assert "Unknown from deterministic fallback" in fallback_summary
    # 4. Historical mentions in dropped turns and tool logs must be marked review_required,
    # NOT active contradictions, because they lack explicit synthetic assertion as active instructions!
    assert score.contradiction_count == 0
    assert score.review_required_count > 0
    assert all(not c.asserted_as_active for c in score.contradiction_results)
    assert any(c.review_required for c in score.contradiction_results)


def test_superseded_fact_classification_old_tool_log_vs_wrong_host_vs_negation():
    """Verify classification across:
    1. Old quoted tool log -> review_required (NOT active contradiction)
    2. Explicit current wrong host -> asserted_as_active (active contradiction)
    3. Correct negation -> negated (clean)
    """
    gt = MIGRATION_CASE_GROUND_TRUTH

    # Case 1: Old quoted tool log (e.g. '[terminal] ran old-host probe: nc -zv pg-legacy-01.internal 5432')
    tool_log_summary = (
        "## Completed Actions\n"
        "1. [terminal] ran old-host probe: nc -zv pg-legacy-01.internal 5432 -> exit 0\n"
        "## Active State\n"
        "Working on migration.\n"
    )
    score_tool_log = evaluate_summary(tool_log_summary, gt)
    tool_log_c = {c.superseded_fact: c for c in score_tool_log.contradiction_results}
    assert tool_log_c["pg-legacy-01.internal"].present is True
    assert tool_log_c["pg-legacy-01.internal"].asserted_as_active is False
    assert tool_log_c["pg-legacy-01.internal"].review_required is True
    assert tool_log_c["pg-legacy-01.internal"].verdict == "review_required"
    assert tool_log_c["5432"].asserted_as_active is False
    assert tool_log_c["5432"].review_required is True
    assert score_tool_log.contradiction_count == 0
    assert score_tool_log.contradiction_penalty == 0.0

    # Case 2: Explicit current wrong host (asserting superseded host as active target)
    wrong_host_summary = (
        "## Constraints & Preferences\n"
        "- Connect to database server at pg-legacy-01.internal on port 5432 using user pg_legacy_admin.\n"
        "## Active State\n"
        "Active target: pg-legacy-01.internal:5432\n"
    )
    score_wrong_host = evaluate_summary(wrong_host_summary, gt)
    wrong_c = {c.superseded_fact: c for c in score_wrong_host.contradiction_results}
    assert wrong_c["pg-legacy-01.internal"].present is True
    assert wrong_c["pg-legacy-01.internal"].asserted_as_active is True
    assert wrong_c["pg-legacy-01.internal"].review_required is False
    assert wrong_c["pg-legacy-01.internal"].verdict == "asserted_active"
    assert wrong_c["5432"].asserted_as_active is True
    assert score_wrong_host.contradiction_count >= 2
    assert score_wrong_host.contradiction_penalty > 0.0

    # Case 3: Correct negation
    negated_summary = (
        "## Constraints & Preferences\n"
        "- SUPERSEDED / DO NOT USE: pg-legacy-01.internal on port 5432 with pg_legacy_admin is frozen and decommissioned.\n"
        "## Active State\n"
        "Active target: aurora-pg-prod.vpc-east.internal:5439\n"
    )
    score_negated = evaluate_summary(negated_summary, gt)
    neg_c = {c.superseded_fact: c for c in score_negated.contradiction_results}
    assert neg_c["pg-legacy-01.internal"].present is True
    assert neg_c["pg-legacy-01.internal"].asserted_as_active is False
    assert neg_c["pg-legacy-01.internal"].review_required is False
    assert neg_c["pg-legacy-01.internal"].verdict == "negated"
    assert neg_c["5432"].asserted_as_active is False
    assert neg_c["5432"].review_required is False
    assert score_negated.contradiction_count == 0
    assert score_negated.contradiction_penalty == 0.0
    assert score_negated.review_required_count == 0


def test_no_heuristic_claim_general_semantic_correctness():
    """Verify that the rubric explicitly refrains from claiming general semantic correctness
    and marks unasserted historical mentions as review_required without arbitrary penalty.
    """
    gt = MIGRATION_CASE_GROUND_TRUTH
    ambiguous_summary = (
        "## Completed Actions\n"
        "1. ran command involving pg-legacy-01.internal\n"
        "## Active State\n"
        "Proceeding with migration.\n"
    )
    score = evaluate_summary(ambiguous_summary, gt)
    ambig_c = next(c for c in score.contradiction_results if c.superseded_fact == "pg-legacy-01.internal")

    # Heuristic does NOT pretend to know whether this is semantically contradictory
    assert ambig_c.verdict == "review_required"
    assert ambig_c.asserted_as_active is False
    assert ambig_c.review_required is True
    assert score.contradiction_penalty == 0.0
    assert any("Semantic contradiction verdict remains manual/LLM unknown" in note for note in score.diagnostic_notes)


def test_production_compaction_growth_guard_offline():
    """Execute the actual production growth guard offline on the synthetic fallback builder result.

    Verifies:
    1. Candidate output is larger than input (would grow the conversation).
    2. Actual production growth guard refuses compaction: rejected_would_grow=True.
    3. Original transcript is preserved: original_preserved=True.
    4. Proves runtime DOES NOT expand the conversation.
    """
    transcript = SYNTHETIC_MIGRATION_TRANSCRIPT
    guard = execute_production_growth_guard_offline(transcript)

    assert guard.rough_candidate_tokens > guard.rough_input_tokens
    assert guard.salvaged is False
    assert guard.rejected_would_grow is True
    assert guard.original_preserved is True
    assert guard.accepted_messages is None


def test_production_accepted_state_evaluation_refused():
    """Verify evaluation of the true accepted state when growth guard refuses compaction."""
    from evals.compaction.summary_usefulness.harness import (
        execute_production_growth_guard_offline,
        evaluate_production_accepted_state,
    )
    transcript = SYNTHETIC_MIGRATION_TRANSCRIPT
    gt = MIGRATION_CASE_GROUND_TRUTH

    guard = execute_production_growth_guard_offline(transcript)
    assert guard.rejected_would_grow is True
    assert guard.original_preserved is True

    # True final accepted state is the preserved original transcript
    score = evaluate_production_accepted_state(transcript, guard, gt)
    assert score.matched_required_facts == score.total_required_facts
    assert score.unfinished_intent_retained is True
    assert score.decisions_retained == score.decisions_total
    assert score.usefulness_score >= 0.6
