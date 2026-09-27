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
   - **Active Contradiction**: Only triggered when a summary contains an **explicit synthetic assertion** directing the agent to use or connect to a superseded/deprecated parameter (e.g., `"Connect to database server at pg-legacy-01.internal on port 5432"`).
   - **Historical References (`review_required`)**: When a superseded parameter appears in a historical tool execution log (e.g., `[terminal] ran old-host...`, `[tool]`, completed action entries, or dropped turns), it is marked as `review_required`, **not** an active contradiction.
   - **No Semantic Overclaim**: Because keyword proximity cannot determine semantic intent, the semantic contradiction verdict for `review_required` references remains manual/LLM unknown.

4. **Intent & Dead-End Memory**:
   - **Unfinished Intent**: Must retain pending user instructions and must not falsely mark tasks as completed.
   - **Failed Approaches**: Must record technical failures (e.g. MTU packet mismatch socket errors) so the agent does not loop on dead-ends.
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
PYTHONPATH=. pytest evals/compaction/summary_usefulness/test_summary_usefulness.py -v
```

---

## Live Evaluation Runner Contract & Prior Run Audit

`live_runner.py` provides an evaluation runner for testing summary usefulness against configured auxiliary models.

### Prior Run Actuals (Transparent Accounting)
An initial live test was executed using `gemini-3.8-flash-high` via Google OAuth / Antigravity Gateway on the synthetic expanded fixture. A contract audit revealed key realities that must never be misclaimed:
- **Output Token Cap Was NOT Met**: The prior run generated **3,696 completion tokens** (`provider_completion_tokens: 3696`), far exceeding the intended 1,500-token cap. This occurred because `live_runner.py` called `call_llm` without passing `max_tokens=1500`.
- **Summary Expanded After Augmentation**:
  - Raw synthetic model output: 10,547 characters
  - Cleaned / augmented summary: 14,224 characters
  - Context shrink ratio was 1.1792 (an expansion rather than a reduction).
- **No Accepted Runtime Compaction Verified**: This evaluation tested prompt construction, raw model completion, and production post-cleaning in isolation. It did **not** verify or commit an accepted compaction inside Hermes's runtime (`agent.conversation_compression._salvage_or_refuse_grown_transcript`). Acceptance of this live candidate was not tested through the full runtime guard, including salvage and retained-tail handling.
- **Logical Request vs. Wire Count**: The runner dispatches **one logical auxiliary request** via `call_llm`. Because `call_llm` manages transient retries internally, transport blips may trigger same-provider retries under the hood; the runner does not guarantee an exact single physical wire request.
- **Endpoint Scrubbing Replaced by Omission**: Previously, bespoke string scrubbing attempted to remove `key=` parameters. Endpoints and URLs are now completely omitted from returned metadata to eliminate any risk of leaking sensitive gateway paths or query strings.

### Enforced Contract Guarantees in `live_runner.py`
1. **Input Refusal (Fail Closed > 8,000 tokens)**: `exact_input_tokens` is measured via `count_tokens` before dispatching. If it exceeds 8,000 tokens, a `ValueError` is raised immediately, failing closed before any inference occurs.
2. **Explicit Output Bound (`max_tokens=1500`)**: `max_tokens=1500` (or caller-specified `max_output_tokens`) is passed directly to `call_llm`, which enforces the output token budget on the provider request.
3. **Honest Call Accounting**: The runner explicitly documents that it executes one logical request, acknowledging internal retry behavior in `call_llm`.
4. **Complete Endpoint Exclusion**: Metadata contains no endpoint or URL fields (`client_endpoint` omitted entirely).
5. **Canonical Dynamic Scratch Directory**: Scratch path defaults to runtime `TMPDIR` if set, or canonical `hermes_constants.get_scratch_dir()` / `get_hermes_home() / "cache" / "scratch"`. No hardcoded paths (`Path.home()`) or ad-hoc environment variables (`HERMES_SCRATCH_DIR`).
