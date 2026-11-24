"""Offline triage for redacted trajectory JSONL; only fixed evidence is emitted."""

import json
from pathlib import Path

FAILURE_CODES = {
    "provider_error",
    "tool_error",
    "tool_refusal",
    "interrupted",
    "compression_timeout",
}
MAP = {
    "completed": (
        "agent/trajectory.py",
        "tests/agent/test_trajectory_persistence_redaction.py",
    ),
    "provider_error": (
        "agent/turn_api_error.py",
        "tests/agent/test_timeout_error_summary.py",
    ),
    "tool_error": ("model_tools.py", "tests/agent/test_display_tool_failure.py"),
    "tool_refusal": (
        "model_tools.py",
        "tests/tools/test_hosted_deny_by_default.py",
    ),
    "interrupted": (
        "agent/turn_liveness.py",
        "tests/agent/test_sequential_tool_interrupt.py",
    ),
    "compression_timeout": (
        "agent/compression_facade.py",
        "tests/agent/test_compression_timeout_terminal.py",
    ),
}


def _references(label, root):
    def available(relative):
        path = (root / relative).resolve()
        return [relative] if path.is_relative_to(root) and path.is_file() else []

    source, test = MAP[label]
    return available(source), available(test)


def classify(row):
    if not isinstance(row, dict) or not isinstance(row.get("conversations"), list):
        return "malformed", {"valid_record": False}
    completed = row.get("completed")
    if not isinstance(completed, bool):
        return "malformed", {"valid_record": False}
    observed = {"completed": completed}
    if completed is True:
        return "completed", observed
    metadata = row.get("metadata")
    code = metadata.get("failure_code") if isinstance(metadata, dict) else None
    if isinstance(code, str) and code in FAILURE_CODES:
        observed["evidence_basis"] = "explicit_metadata"
        return code, observed
    counts = row.get("tool_error_counts")
    if isinstance(counts, dict):
        kinds = sum(type(v) in (int, float) and v > 0 for v in counts.values())
        if kinds:
            observed["positive_tool_error_kinds"] = kinds
            return "tool_error", observed
    observed["failure_evidence"] = "insufficient"
    return "unknown", observed


def iter_reports(filename, root):
    root = Path(root).resolve()
    references = {label: _references(label, root) for label in MAP}
    with open(filename, "rb") as stream:
        for number, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                line = raw.decode("utf-8")
                row = json.loads(line)
                label, observed = classify(row)
            except (ValueError, RecursionError):
                label, observed = "malformed", {"valid_record": False}
            sources, tests = references.get(label, ([], []))
            yield {
                "line": number,
                "diagnosis": label,
                "observed": observed,
                "sources": sources,
                "tests": tests,
            }
