# Summary Usefulness Evaluation (Heuristic Proxy Benchmark)

Evaluates the **factual retention, superseded-parameter handling, and growth-guard behavior** of context compaction summaries, decoupled from token shrinkage tests.

> **CRITICAL DISCLAIMER: Heuristic Proxy Only — No Semantic Usefulness Claim**
> Deterministic keyword, identifier, and regular-expression matching provides **heuristic proxy indicators** only. It does **not** prove general semantic comprehension, actionability, or model intelligence. General semantic evaluation is reserved for human or model-as-a-judge evaluation.

---

## Core Rubric & Principles

1. **Orthogonal Shrink Ratio (Separate from Fact Retention)**:
   Token shrinkage is tracked purely as an efficiency metric. High token shrinkage does **not** increase usefulness; a summary that shrinks context by 85% by dropping all specific identifiers receives a proxy usefulness score of 0. Fact retention and shrink ratio are reported independently.

2. **Deterministic Checks for Exact Facts**:
   Exact deterministic checks apply **only** to exact, non-negotiable facts:
   - Identifiers & parameters (ports, hostnames, usernames, database/table names).
   - Exact constraints (e.g. security policy `SEC-9941`, forbidden flags like `--no-owner`, required SSL modes like `verify-full`).
   - Checksums and hashes (`e3b0c44298fc...`).
   - Absolute file and directory paths (`/etc/ssl/certs/...`, `/var/scratch/...`).

3. **Superseded Parameters: Active Contradiction vs `review_required`**:
   - **Active Contradiction**: Only triggered when a summary contains an **explicit synthetic assertion** directing the agent to use or connect to a superseded/deprecated parameter (e.g., `"Connect to database server at pg-legacy-01.internal on port 5432"`) in an active directive context.
   - **Historical References (`review_required`)**: When a superseded parameter appears in a historical tool execution log (e.g., `[terminal] ran old-host...`, `[tool]`, completed action entries, dropped turns, or past-tense/ambiguous framing like `initially planned to connect to...`), it is marked as `review_required`, **not** an active contradiction.
   - **No Semantic Overclaim**: Because keyword proximity cannot determine semantic intent, ambiguous histories are marked `review_required` with **no semantic guarantee**.

4. **Intent & Dead-End Memory**:
   - **Unfinished Intent**: Must retain pending user instructions and must not falsely mark tasks as completed.
   - **Failed Approaches (Multi-Occurrence Search)**: Searches all occurrences of a failed approach across the summary so that an initial non-failing mention (e.g. in Goal or Completed Actions) does not mask a subsequent failure explanation (e.g. in Blocked or Key Decisions). Mentions lacking failure markers are flagged `review_required` with no semantic guarantee.
   - **Key Decisions**: Must record technical choices and rationale (e.g., chunked batch cursor with RocksDB checkpointing).

5. **Formal Semantic Judge Specification**:
   For human or LLM evaluators, `SEMANTIC_JUDGE_RUBRIC_SPEC` defines 4 semantic axes (Score 1 to 5 each):
   - **Actionable Continuity** (1-5)
   - **State & Constraint Fidelity** (1-5)
   - **Failure Memory & Dead-End Avoidance** (1-5)
   - **Structural Cleanliness** (1-5)

---

## Distinction: RAW Fallback Builder Output vs. Production Accepted Compaction

A critical distinction exists between raw builder output and production runtime behavior:

1. **RAW Fallback Builder Output (`ContextCompressor._build_static_fallback_summary`)**:
   - When the LLM summarizer is unreachable offline, the fallback builder deterministically extracts anchors (user asks, completed tool commands, dropped turn history) into a structured markdown document.
   - Because it quotes earlier turns and tool logs verbatim, this raw string can be larger than the short input transcript (e.g. ~2,107 tokens vs ~853 tokens on synthetic test cases).

2. **Production Accepted Compaction (Runtime Anti-Growth Guard)**:
   - In production (`agent.conversation_compression._salvage_or_refuse_grown_transcript`), the runtime executes an anti-growth guard prior to committing any compaction.
   - When the candidate compressed transcript exceeds the original token count and salvage cannot shrink it below the budget, the runtime:
     - Sets `rejected_would_grow = True`
     - Preserves the original transcript unchanged (`original_preserved = True`)
     - Refuses compaction commit.
   - **Tested behavior**: The exercised offline growth-guard path rejects the expanding synthetic candidate; this does not establish every runtime path.

---

## Fixtures

All fixtures in `fixtures.py` are explicitly marked as **SYNTHETIC**:
- `SYNTHETIC_MIGRATION_TRANSCRIPT`: Multi-turn interaction containing initial setup, user correction of host/port, network MTU failure, technical decision, and in-flight unfinished phase 1 task.
- `MIGRATION_CASE_GROUND_TRUTH`: Ground-truth specification listing active facts, superseded facts, failed approaches, decisions, and pending tasks.
- `SYNTHETIC_HANDCRAFTED_GOOD_SUMMARY`: Reference synthetic summary demonstrating high usefulness, exact constraint preservation, and explicit marking of superseded facts.
- `SYNTHETIC_HANDCRAFTED_BAD_CONTRADICTORY_SUMMARY`: Synthetic summary demonstrating regression to superseded facts via explicit active assertions.
- `SYNTHETIC_HANDCRAFTED_BAD_VAGUE_SUMMARY`: Synthetic summary demonstrating high token shrinkage but near-zero actionable usefulness.

---

## Running the Evaluation

```bash
# Run benchmark matrix and production growth guard verification
PYTHONPATH=. python3 -m evals.compaction.summary_usefulness.harness

# Run automated unit tests
PYTHONPATH=. pytest evals/compaction/summary_usefulness/test_summary_usefulness.py evals/compaction/summary_usefulness/test_live_runner.py -v
```

---

## Live Evaluation Runner Contract & Prior Run Audit

`live_runner.py` provides a faithful evaluation runner exercising the actual production compaction pipeline against configured auxiliary models or offline mocks.

### Prior Run Actuals & Historical Flaw Audit
An initial live test was executed using `gemini-3.8-flash-high` via Google OAuth / Antigravity Gateway on the synthetic expanded fixture. A contract audit revealed critical discrepancies that have now been corrected:
- **Output Token Cap Was NOT Met**: The prior run generated **3,696 completion tokens** (`provider_completion_tokens: 3696`), far exceeding the intended 1,500-token cap, because `live_runner.py` invoked `call_llm` without passing `max_tokens=1500`.
- **Mismatched Production Window**: The prior runner manually gathered all non-system turns into the summarizer prompt, completely bypassing production windowing (`_compress_window`), which protects the conversation head and token-budget tail.
- **Ignored Length & Manual Augmentation Stub**: When output truncated or hit caps, the prior runner ignored `finish_reason == "length"` and manually ran post-processing (`_augment_summary_lean`) to stitch together a synthetic stub summary. In production, length truncation raises a terminal truncation error and aborts compression, preserving the original transcript without generating an augmentation stub.
- **Unverified Compaction & Mismatched Scoring**: The prior run did not verify or commit an accepted compaction through the commit-site anti-growth guard (`_salvage_or_refuse_grown_transcript`). It scored the manually assembled string in isolation, rather than capturing and scoring the final accepted messages distinctly from raw LLM output.
- **Refusal Honesty**: Refusal or abort by the growth guard preserves the original transcript unchanged. While the preserved original transcript retains all original facts, this is non-expansion honesty, NOT a successful compaction.

### Faithful Architecture & Guarantees in `live_runner.py`
1. **Exercises Actual Production `compress()` Pipeline**:
   Calls `ContextCompressor.compress()`, faithfully executing:
   `_compress_window` -> `_sample_summary_records` -> `_build_summary_prompt` -> `_call_summary_llm` -> `_assemble_compressed` -> `_salvage_or_refuse_grown_transcript`.
2. **Default Offline / Fail-Closed**:
   Guarantees zero accidental network or live inference calls. Fails closed before route resolution, config lookup, or token counting unless explicitly authorized via `allow_live=True` or provided with an explicit offline `mock_response` fixture. Monkeypatching aliases does not authorize offline execution without an explicit mock fixture.
3. **Strict Input Token Bound (<= 8,000 tokens)**:
   Measures and gates prompt tokens on both the prebuilt window prompt and the exact ACTUAL prompt at dispatch at the transport interceptor (using offline deterministic counter or live client counter, not stale prebuilt). If input tokens exceed 8,000, raises `ValueError` immediately, failing closed before or at inference dispatch.
4. **Honest Output Budget (Opt-In vs Production Default)**:
   In production, `_call_summary_llm` does not pass `max_tokens` (honest production default). Experimental caps (e.g. `max_output_tokens=1500`) are strictly opt-in and passed directly to the auxiliary transport.
5. **Bounded Physical Auxiliary Calls at Verified SDK Send Seam**:
   Enforces a strict upper bound on physical transport calls at the verified SDK send seam (`route.client.chat.completions.create`), accurately intercepting and bounding internal retries within `call_llm` (`max_auxiliary_calls=1` by default). Fails closed immediately without entering unbounded fallback loops.
6. **Production Length & Refusal Rejection**:
   When `finish_reason == "length"` or model refusal occurs, production `_call_summary_llm` raises an error, aborting compression and preserving the original transcript without creating an augmentation stub.
7. **Commit-Site Anti-Growth Guard**:
   Passes candidate messages through `_salvage_or_refuse_grown_transcript`. If the candidate would grow the conversation and salvage cannot shrink it below the pre-compaction budget, commit is refused (`rejected_would_grow = True`) and the original transcript is preserved.
8. **Distinct Capture & Scoring**:
   Captures `original_messages`, `candidate_messages`, and `final_accepted_messages`. Evaluates the final accepted state distinctly from raw LLM output.
9. **Zero Endpoint / Secret Leakage**:
   Metadata completely omits endpoint URLs, hostnames, and credentials.
10. **Canonical Exclusive Scratch Directory**:
    Artifacts land in `compaction-eval-fidelity-fix` under dynamic `TMPDIR` or canonical scratch directory.
