# Offline harness comparison

This utility validates a task manifest and summarizes recorded JSONL runs. It is
offline: it does not launch models, providers, or artifact validators. A task
validator is an inert caller-supplied label.

## Commands

From the repository root, run `python -m evals.harness_compare validate --manifest M`
to validate a manifest, or `python -m evals.harness_compare report --manifest M
--results R` to print a JSON report. Invalid input exits nonzero with a diagnostic.

## Fixtures

A manifest is a JSON object with string `model` and `config` labels, a `budget`
object containing positive integer `max_tokens`, and `tasks`. Each task has a
unique immutable string `id` and a `split` of `development`, `held_out`, or
`replay`; optional `validator` is a descriptive label only. The task owner assigns
splits: development is for iteration, held_out is reserved for evaluation, and
replay identifies replay-only cases. IDs cannot occur more than once, including
across splits.

Results are JSONL objects with `task_id`, boolean `objective_pass`, and optional
`failure`, `judge`, `tokens` (nonnegative integer), and `cost` (nonnegative number).
Rows represent runs, so repeated task IDs are preserved. Optional descriptive
fields can carry structured JSON values. Shared reports preserve the supplied failure and judge details, so use redacted descriptors. Reports include the model, config and validated maximum token budget. Reports count observed runs by manifest
split, list each recorded failure and judge with its task ID, and report objective
pass/fail separately. Missing usage measurements remain unavailable: a metric's
split total is null if any observed run lacks it; malformed supplied measurements
are rejected. A split with no observed runs has zero runs and zero metric totals.

Example manifest (one task per split):

```json
{"model":"local","config":"baseline","budget":{"max_tokens":100},"tasks":[{"id":"d1","split":"development"},{"id":"h1","split":"held_out"},{"id":"r1","split":"replay","validator":"artifact-check"}]}
```

Example results (development pass: 4 tokens/$0.10; held-out failure: 6 tokens
with unknown cost; replay pass with no measurements):

```jsonl
{"task_id":"d1","objective_pass":true,"tokens":4,"cost":0.1}
{"task_id":"h1","objective_pass":false,"failure":"assertion","judge":{"note":"needs review"},"tokens":6}
{"task_id":"r1","objective_pass":true}
```

The development totals are runs=1, passed=1, failed=0, tokens=4, cost=0.1;
held-out totals are runs=1, passed=0, failed=1, tokens=6, cost=null; replay
totals are runs=1, passed=1, failed=0, tokens=null, cost=null. A model/config
comparison compares each whole configuration; it cannot by itself establish
component-level causality.
