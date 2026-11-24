import json
from pathlib import Path

import pytest

from evals.trajectory_triage.report import classify, iter_reports

ROOT = Path(__file__).resolve().parents[2]


def record(completed=False, **fields):
    return {"conversations": [], "completed": completed, **fields}


@pytest.mark.parametrize(
    "code",
    [
        "provider_error",
        "tool_error",
        "tool_refusal",
        "interrupted",
        "compression_timeout",
    ],
)
def test_explicit_failure_codes_and_terminal_status(code):
    assert classify(record(metadata={"failure_code": code}))[0] == code
    assert classify(record(True, metadata={"failure_code": code}))[0] == "completed"


def test_untrusted_content_and_errors_do_not_enter_output(tmp_path):
    secret = "private-fixture-input"
    data = tmp_path / "trace.jsonl"
    lines = [
        json.dumps(record(tool_error_counts={secret: 1})).encode(),
        b'{"invalid":',
        bytes([255]),
        json.dumps(
            record(
                conversations=[{"from": "human", "value": secret + " provider_error"}]
            )
        ).encode(),
        json.dumps({"metadata": {"failure_code": "provider_error"}}).encode(),
        json.dumps(record(metadata={"failure_code": "compression_timeout"})).encode(),
    ]
    data.write_bytes(b"\n".join(lines))
    reports = list(iter_reports(data, ROOT))
    assert [r["line"] for r in reports] == list(range(1, 7))
    assert [r["diagnosis"] for r in reports] == [
        "tool_error",
        "malformed",
        "malformed",
        "unknown",
        "malformed",
        "compression_timeout",
    ]
    assert secret not in json.dumps(reports)
    assert "agent/compression_facade.py" in reports[-1]["sources"]
    assert "tests/agent/test_compression_timeout_terminal.py" in reports[-1]["tests"]


def test_missing_source_does_not_move_test_into_sources(tmp_path):
    test_path = "tests/agent/test_compression_timeout_terminal.py"
    target = tmp_path / test_path
    target.parent.mkdir(parents=True)
    target.touch()
    data = tmp_path / "trace.jsonl"
    data.write_text(
        json.dumps(record(metadata={"failure_code": "compression_timeout"}))
    )
    [report] = list(iter_reports(data, tmp_path))
    assert report["sources"] == []
    assert report["tests"] == [test_path]
