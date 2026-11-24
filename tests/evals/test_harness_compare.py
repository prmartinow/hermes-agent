import json
import subprocess
import sys

import pytest

from evals.harness_compare.report import InputError, build_report, validate_manifest


def manifest():
    return {
        "model": "local",
        "config": "baseline",
        "budget": {"max_tokens": 10},
        "tasks": [
            {"id": "a", "split": "development"},
            {"id": "b", "split": "held_out"},
            {"id": "c", "split": "replay"},
        ],
    }


def test_validate_manifest():
    assert validate_manifest(manifest()) == {
        "a": "development",
        "b": "held_out",
        "c": "replay",
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m["tasks"].append({"id": "a", "split": "replay"}),
        lambda m: m["tasks"][0].update(split="train"),
        lambda m: m["budget"].update(max_tokens=True),
    ],
)
def test_bad_manifest(change):
    value = manifest()
    change(value)
    with pytest.raises(InputError):
        validate_manifest(value)


def test_report_uses_runs_and_preserves_missing_metrics(tmp_path):
    mapping = validate_manifest(manifest())
    path = tmp_path / "runs.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "task_id": "a",
                    "objective_pass": True,
                    "tokens": 4,
                    "cost": 1.5,
                    "judge": "ok",
                },
                {
                    "task_id": "a",
                    "objective_pass": False,
                    "tokens": 3,
                    "cost": 2.0,
                    "failure": "timeout",
                },
                {
                    "task_id": "b",
                    "objective_pass": False,
                    "cost": 0.5,
                    "judge": {"score": 1},
                },
            ]
        )
    )
    report = build_report(mapping, path)["splits"]
    assert report["development"] == {
        "runs": 2,
        "passed": 1,
        "failed": 1,
        "tokens": 7,
        "cost": 3.5,
        "judges": [{"task_id": "a", "judge": "ok"}],
        "failures": [{"task_id": "a", "failure": "timeout"}],
    }
    assert report["held_out"]["runs"] == 1
    assert report["held_out"]["tokens"] is None
    assert report["held_out"]["cost"] == 0.5
    assert report["held_out"]["judges"] == [{"task_id": "b", "judge": {"score": 1}}]
    assert report["held_out"]["failures"] == []
    assert report["replay"]["runs"] == 0 and report["replay"]["tokens"] == 0


def test_cli_validate_and_report(tmp_path):
    m = tmp_path / "manifest.json"
    data = manifest()
    data["budget"]["private_note"] = "private-budget-fixture"
    m.write_text(json.dumps(data))
    r = tmp_path / "runs.jsonl"
    r.write_text(
        json.dumps({"task_id": "c", "objective_pass": True, "tokens": 2, "cost": 0})
        + "\n"
    )
    validate = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.harness_compare",
            "validate",
            "--manifest",
            str(m),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.harness_compare",
            "report",
            "--manifest",
            str(m),
            "--results",
            str(r),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert (report["model"], report["config"]) == ("local", "baseline")
    assert report["budget"] == {"max_tokens": 10}
    assert "private-budget-fixture" not in result.stdout
    assert report["splits"]["replay"]["passed"] == 1


def test_unknown_task_rejected(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(json.dumps({"task_id": "missing", "objective_pass": True}) + "\n")
    with pytest.raises(InputError, match="unknown manifest task"):
        build_report(validate_manifest(manifest()), path)


@pytest.mark.parametrize(
    "line",
    [
        "[]",
        "null",
        '{"task_id":"a","objective_pass":true,"tokens":-1}',
        '{"task_id":"a","objective_pass":true,"cost":"unknown"}',
    ],
)
def test_malformed_result_rows_have_diagnostics(tmp_path, line):
    path = tmp_path / "bad.jsonl"
    path.write_text(line + "\n")
    with pytest.raises(InputError):
        build_report(validate_manifest(manifest()), path)
