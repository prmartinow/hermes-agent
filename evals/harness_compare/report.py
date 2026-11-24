"""Validate split membership and aggregate recorded runs using only their supplied measurements."""

import json
import math
from pathlib import Path

SPLITS = {"development", "held_out", "replay"}


class InputError(ValueError):
    pass


def _object(value, where):
    if not isinstance(value, dict):
        raise InputError(f"{where} must be an object")


def validate_manifest(data):
    _object(data, "manifest")
    for key in ("model", "config"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise InputError(f"manifest.{key} must be a string")
    budget = data.get("budget")
    _object(budget, "manifest.budget")
    tokens = budget.get("max_tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise InputError("manifest.budget.max_tokens must be a positive integer")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise InputError("manifest.tasks must be a non-empty array")
    ids = {}
    for i, task in enumerate(tasks):
        where = f"manifest.tasks[{i}]"
        _object(task, where)
        task_id, split = task.get("id"), task.get("split")
        if not isinstance(task_id, str) or not task_id.strip():
            raise InputError(f"{where}.id must be a non-empty string")
        if task_id in ids:
            raise InputError(f"{where}.id duplicates task ID {task_id!r}")
        if not isinstance(split, str) or split not in SPLITS:
            raise InputError(
                f"{where}.split must be one of development, held_out, replay"
            )
        if "validator" in task and not isinstance(task["validator"], str):
            raise InputError(f"{where}.validator must be a string")
        ids[task_id] = split
    return ids


def _result(line, where, splits):
    try:
        row = json.loads(line)
    except (ValueError, UnicodeError) as exc:
        raise InputError(f"{where}: invalid JSON") from exc
    _object(row, where)
    task = row.get("task_id")
    if not isinstance(task, str) or task not in splits:
        raise InputError(f"{where}: unknown manifest task {task!r}")
    if not isinstance(row.get("objective_pass"), bool):
        raise InputError(f"{where}.objective_pass must be a boolean")
    tokens = row.get("tokens")
    if tokens is not None and (type(tokens) is not int or tokens < 0):
        raise InputError(f"{where}.tokens must be a nonnegative integer")
    cost = row.get("cost")
    if cost is not None and (
        type(cost) not in (int, float)
        or cost < 0
        or isinstance(cost, float)
        and not math.isfinite(cost)
    ):
        raise InputError(f"{where}.cost must be a finite nonnegative number")
    return row


def build_report(task_splits, results_path):
    totals = {
        split: {
            "runs": 0,
            "passed": 0,
            "failed": 0,
            "tokens": 0,
            "cost": 0,
            "judges": [],
            "failures": [],
        }
        for split in sorted(SPLITS)
    }
    with Path(results_path).open("rb") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            row = _result(line, f"{results_path}:{number}", task_splits)
            task = row["task_id"]
            split = totals[task_splits[task]]
            split["runs"] += 1
            split["passed" if row["objective_pass"] else "failed"] += 1
            for key in ("tokens", "cost"):
                if row.get(key) is None:
                    split[key] = None
                elif split[key] is not None:
                    split[key] += row[key]
            for key in ("judge", "failure"):
                if key in row:
                    split[key + "s"].append({"task_id": task, key: row[key]})
    return {"splits": totals}
