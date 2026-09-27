"""Rubric and scoring engine for summary usefulness evaluation.

Core principles:
1. Retention and contradiction/hallucination are measured independently of shrink ratio.
   Token shrinkage is an efficiency metric, never conflated with factual usefulness.
2. Exact deterministic checks apply ONLY to exact facts (identifiers, paths, ports,
   hashes, constraints).
3. Metric scores are explicitly labeled as HEURISTIC PROXIES, NOT general semantic
   usefulness claims. Deterministic pattern checks cannot prove comprehension.
4. Historical references to superseded parameters (e.g. in tool logs like
   '[terminal] ran old-host...', dropped turns, or completed action logs) are marked
   as `review_required`, NOT active contradictions, unless supported by an explicit
   synthetic assertion directing active usage. The general semantic contradiction
   verdict remains manual/LLM unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class ExactFactResult:
    fact: str
    found: bool
    matched_snippet: Optional[str] = None


@dataclass
class ContradictionResult:
    superseded_fact: str
    present: bool
    asserted_as_active: bool
    review_required: bool = False
    verdict: str = "absent"  # "absent", "negated", "asserted_active", "review_required"
    context_snippet: Optional[str] = None


@dataclass
class SummaryUsefulnessScore:
    # 1. Deterministic exact retention (orthogonal to shrink ratio)
    total_required_facts: int
    matched_required_facts: int
    exact_retention_score: float  # [0.0, 1.0]

    # 2. Contradiction / Superseded fact penalty
    total_superseded_checked: int
    contradiction_count: int  # Only explicit synthetic assertions as active instructions
    contradiction_penalty: float  # [0.0, 1.0]
    review_required_count: int  # Superseded facts referenced ambiguously/historically where semantic verdict is unknown

    # 3. Intent & Decision preservation
    unfinished_intent_retained: bool
    unfinished_intent_score: float  # [0.0, 1.0]
    decisions_retained: int
    decisions_total: int
    decision_score: float  # [0.0, 1.0]
    failed_approaches_retained: int
    failed_approaches_total: int
    failed_approach_score: float  # [0.0, 1.0]

    # 4. Overall heuristic proxy score (independent of shrink ratio)
    # Explicitly labeled as a heuristic proxy, NOT a general semantic usefulness claim.
    usefulness_score: float  # [0.0, 1.0] (heuristic proxy)
    proxy_usefulness_score: float

    # 5. Shrink ratio (orthogonal metric, NOT mixed into usefulness)
    summary_chars: int
    transcript_chars: int
    shrink_ratio: float  # summary / transcript (smaller means more compact)

    # Detailed lists
    fact_results: List[ExactFactResult] = field(default_factory=list)
    contradiction_results: List[ContradictionResult] = field(default_factory=list)
    diagnostic_notes: List[str] = field(default_factory=list)


NEGATION_PATTERNS: List[str] = [
    r"\bsuperseded\b",
    r"\bdo\s+not\s+use\b",
    r"\bdeprecated\b",
    r"\bfrozen\b",
    r"\bdecommissioned\b",
    r"\bavoid\b",
    r"\bstop\b",
    r"\bnever\b",
    r"\bdo\s+not\s+touch\b",
    r"\babandoned\b",
    r"\breplaced\b",
    r"\bobsolete\b",
    r"\bno\s+longer\b",
    r"\b(?:correction|corrected)\b",
]

HISTORICAL_SECTIONS: List[str] = [
    "historical user task",
    "completed actions",
    "last dropped turns",
    "user messages",
    "previous summary snapshot",
    "errors & fixes",
]

EXPLICIT_ASSERTION_PATTERNS: List[str] = [
    r"\bconnect\s+(?:to\s+)?(?:database\s+server\s+at\s+)?",
    r"\bserver\s+at\s+",
    r"\bdatabase\s+at\s+",
    r"\btarget\s+(?:host|server|database)?[:\s]+",
    r"\bactive\s+(?:target|host|endpoint)[:\s]+",
    r"\b(?:use|using)\s+(?:user\s+)?",
    r"\bon\s+port\s+",
]


def _get_current_markdown_section(text: str, pos: int) -> str:
    """Find the markdown H2 heading preceding the given character index."""
    matches = list(re.finditer(r"(?m)^##\s+(.+)$", text[:pos]))
    if matches:
        return matches[-1].group(1).lower().strip()
    return ""


class SummaryUsefulnessRubric:
    """Evaluates summary usefulness using strict separation between exact facts,
    contradiction penalties, review-required flags, and shrink ratio.
    """

    def __init__(self, ground_truth: Dict[str, Any]):
        self.gt = ground_truth
        self.case_id = ground_truth.get("case_id", "unspecified")
        self.required_facts = ground_truth.get("required_active_facts", [])
        self.superseded_facts = ground_truth.get("superseded_facts", [])
        self.failed_approaches = ground_truth.get("failed_approaches", [])
        self.key_decisions = ground_truth.get("key_decisions", [])
        self.unfinished_tasks = ground_truth.get("unfinished_tasks", [])

    def evaluate(self, summary_text: str, transcript_text: str = "") -> SummaryUsefulnessScore:
        """Run deterministic checks against the summary text.

        Returns heuristic proxy metrics. Does NOT claim general semantic comprehension.
        """
        notes: List[str] = []
        summary_lower = summary_text.lower()

        # 1. Exact fact retention (orthogonal to shrink ratio)
        fact_results: List[ExactFactResult] = []
        matched_facts = 0
        for fact in self.required_facts:
            found = fact.lower() in summary_lower
            snippet = None
            if found:
                matched_facts += 1
                idx = summary_lower.find(fact.lower())
                snippet = summary_text[max(0, idx - 20):min(len(summary_text), idx + len(fact) + 20)].strip()
            fact_results.append(ExactFactResult(fact=fact, found=found, matched_snippet=snippet))

        exact_retention = matched_facts / len(self.required_facts) if self.required_facts else 1.0

        # 2. Contradiction & Superseded facts check
        # A superseded fact is penalizing ONLY IF it is explicitly asserted as an active instruction/target.
        #
        # If it is referenced in historical logs (e.g. "[terminal] ran old-host...", completed actions,
        # dropped turns, quotes) without active assertion, it is labeled review_required (NOT an active
        # contradiction). The general semantic contradiction verdict remains manual/LLM unknown.
        contradiction_results: List[ContradictionResult] = []
        contradiction_count = 0
        review_required_count = 0

        for s_fact in self.superseded_facts:
            fact_lower = s_fact.lower()
            positions = [m.start() for m in re.finditer(re.escape(fact_lower), summary_lower)]

            if not positions:
                contradiction_results.append(ContradictionResult(
                    superseded_fact=s_fact,
                    present=False,
                    asserted_as_active=False,
                    review_required=False,
                    verdict="absent",
                ))
                continue

            has_active_assertion = False
            has_review_required = False
            best_snippet: Optional[str] = None

            for pos in positions:
                w_start = max(0, pos - 100)
                w_end = min(len(summary_text), pos + len(fact_lower) + 100)
                window = summary_lower[w_start:w_end]
                snippet = summary_text[w_start:w_end].strip()
                if best_snippet is None:
                    best_snippet = snippet

                # Check explicit negation
                is_negated = any(re.search(pat, window) for pat in NEGATION_PATTERNS)
                if is_negated:
                    continue

                # Check line & section context
                section = _get_current_markdown_section(summary_text, pos)
                line_start = summary_text.rfind("\n", 0, pos)
                line_start = 0 if line_start == -1 else line_start + 1
                line_end = summary_text.find("\n", pos)
                line_end = len(summary_text) if line_end == -1 else line_end
                line = summary_lower[line_start:line_end].strip()

                is_historical = (
                    any(hs in section for hs in HISTORICAL_SECTIONS)
                    or line.startswith(">")
                    or line.startswith("- user:")
                    or line.startswith("- assistant:")
                    or line.startswith("- tool:")
                    or "[terminal]" in line
                    or "[tool" in line
                    or bool(re.search(r"^\d+\.\s*\[", line))
                    or bool(re.search(r"\bran\s+`", line))
                    or bool(re.search(r"\[terminal\]\s+ran\b", line))
                )

                # Check for explicit synthetic active directive
                active_directive = any(
                    re.search(pat + re.escape(fact_lower), window) for pat in EXPLICIT_ASSERTION_PATTERNS
                )

                if active_directive and not is_historical:
                    has_active_assertion = True
                    best_snippet = snippet
                else:
                    has_review_required = True

            if has_active_assertion:
                contradiction_count += 1
                notes.append(f"Contradiction: Superseded fact '{s_fact}' explicitly asserted as active instruction/target!")
                contradiction_results.append(ContradictionResult(
                    superseded_fact=s_fact,
                    present=True,
                    asserted_as_active=True,
                    review_required=False,
                    verdict="asserted_active",
                    context_snippet=best_snippet,
                ))
            elif has_review_required:
                review_required_count += 1
                notes.append(
                    f"Review required: Superseded fact '{s_fact}' referenced in historical/ambiguous context "
                    f"without explicit active assertion or negation. Semantic contradiction verdict remains manual/LLM unknown."
                )
                contradiction_results.append(ContradictionResult(
                    superseded_fact=s_fact,
                    present=True,
                    asserted_as_active=False,
                    review_required=True,
                    verdict="review_required",
                    context_snippet=best_snippet,
                ))
            else:
                contradiction_results.append(ContradictionResult(
                    superseded_fact=s_fact,
                    present=True,
                    asserted_as_active=False,
                    review_required=False,
                    verdict="negated",
                    context_snippet=best_snippet,
                ))

        # Each active contradiction cuts 0.25 from the proxy score.
        # review_required mentions do NOT incur penalties because semantic verdict is unknown.
        contradiction_penalty = min(1.0, contradiction_count * 0.25)

        # 3. Unfinished intent check
        intent_score = 1.0
        if self.unfinished_tasks:
            falsely_completed = ("none." in summary_lower and "historical user task" in summary_lower) or \
                                "everything was completed" in summary_lower or \
                                "migration finished" in summary_lower
            if falsely_completed:
                intent_score = 0.0
                notes.append("Intent failure: Unfinished task falsely marked completed or None.")
            else:
                hits = 0
                for task in self.unfinished_tasks:
                    words = [w for w in task.lower().split() if len(w) > 4]
                    if any(w in summary_lower for w in words):
                        hits += 1
                intent_score = hits / len(self.unfinished_tasks) if self.unfinished_tasks else 1.0

        # 4. Decisions retention
        if "none recoverable from deterministic fallback" in summary_lower:
            decision_score = 0.0
            dec_matches = 0
            notes.append("Key Decisions: Explicitly unrecoverable from deterministic fallback.")
        elif "## key decisions\nnone" in summary_lower:
            decision_score = 0.0
            dec_matches = 0
        else:
            dec_matches = sum(1 for d in self.key_decisions if any(tok.lower() in summary_lower for tok in d.split()))
            decision_score = dec_matches / len(self.key_decisions) if self.key_decisions else 1.0

        # 5. Failed approaches retention
        fail_matches = 0
        failure_markers = ["fail", "error", "blocked", "abandon", "rollback", "abort"]
        for fa in self.failed_approaches:
            pos = summary_lower.find(fa.lower())
            if pos != -1:
                w_start = max(0, pos - 100)
                w_end = min(len(summary_text), pos + len(fa) + 100)
                w = summary_lower[w_start:w_end]
                if any(m in w for m in failure_markers):
                    fail_matches += 1
                else:
                    notes.append(f"Failed approach '{fa}' mentioned but NOT marked as failed/abandoned!")
        failed_approach_score = fail_matches / len(self.failed_approaches) if self.failed_approaches else 1.0

        # 6. Shrink ratio (tracked independently as an orthogonal metric!)
        summary_len = len(summary_text)
        transcript_len = len(transcript_text) if transcript_text else 1
        shrink_ratio = round(summary_len / max(1, transcript_len), 4)

        # 7. Composite Heuristic Proxy Score (Retention + Continuity - Contradiction, NO SHRINK BIAS)
        # Weights: Exact Retention 45%, Unfinished Intent 25%, Decisions 15%, Failed Approaches 15%, minus Contradiction Penalty
        raw_usefulness = (
            0.45 * exact_retention +
            0.25 * intent_score +
            0.15 * decision_score +
            0.15 * failed_approach_score -
            contradiction_penalty
        )
        usefulness_score = max(0.0, min(1.0, round(raw_usefulness, 4)))

        return SummaryUsefulnessScore(
            total_required_facts=len(self.required_facts),
            matched_required_facts=matched_facts,
            exact_retention_score=round(exact_retention, 4),
            total_superseded_checked=len(self.superseded_facts),
            contradiction_count=contradiction_count,
            contradiction_penalty=round(contradiction_penalty, 4),
            review_required_count=review_required_count,
            unfinished_intent_retained=(intent_score > 0.5),
            unfinished_intent_score=round(intent_score, 4),
            decisions_retained=dec_matches,
            decisions_total=len(self.key_decisions),
            decision_score=round(decision_score, 4),
            failed_approaches_retained=fail_matches,
            failed_approaches_total=len(self.failed_approaches),
            failed_approach_score=round(failed_approach_score, 4),
            usefulness_score=usefulness_score,
            proxy_usefulness_score=usefulness_score,
            summary_chars=summary_len,
            transcript_chars=transcript_len,
            shrink_ratio=shrink_ratio,
            fact_results=fact_results,
            contradiction_results=contradiction_results,
            diagnostic_notes=notes,
        )


# -------------------------------------------------------------------------
# Formal Semantic Evaluation Specification (for Human or LLM Judge)
# Explicitly decoupled from deterministic token/keyword checks.
# -------------------------------------------------------------------------

SEMANTIC_JUDGE_RUBRIC_SPEC = """# Semantic Usefulness Judge Specification (LLM / Human)

NOTE: Exact keyword coverage DOES NOT prove usefulness. A summary might mention
a keyword out of context while completely failing to inform the resuming agent
of what to do next. Deterministic scores are heuristic proxies only.

Evaluate the summary on these 4 semantic axes (Score 1 to 5 each):

1. Actionable Continuity (1-5):
   Can an agent waking up with ONLY this summary and the protected tail immediately
   take the correct next step without repeating past actions or asking the user
   to restate requirements?
   - 5: Perfect clarity on next action, active state, and dependencies.
   - 3: Vague on next action or requires repo rediscovery.
   - 1: Misidentifies active task or falsely assumes work is finished.

2. State & Constraint Fidelity (1-5):
   Are user-mandated constraints (security, flags, forbidden options) and active
   configuration (endpoints, credentials roles, CA certs) presented accurately
   without conflating obsolete/superseded instructions?
   - 5: Explicitly differentiates active vs superseded parameters; quotes strict rules.
   - 3: Drops 1-2 important constraints but avoids dangerous contradictions.
   - 1: Asserts superseded or forbidden configurations as active instructions.

3. Failure Memory & Dead-End Avoidance (1-5):
   Does the summary explain what failed and WHY, preventing an infinite retry loop?
   - 5: Clear post-mortem on failed attempt with error cause and chosen alternative.
   - 3: Notes an error occurred but lacks specific technical cause.
   - 1: Omitted entirely, leaving the agent prone to repeating the failed command.

4. Structural Cleanliness & Reference-Only Framing (1-5):
   Is the summary properly structured under standard headers, free of rambling,
   and framed as reference context rather than hallucinated user directives?
   - 5: Clean markdown conforming to production compaction schema.
   - 1: Disorganized, missing crucial sections, or breaks out of reference framing.
"""
