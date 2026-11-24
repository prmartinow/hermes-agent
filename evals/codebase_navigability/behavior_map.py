"""Validate checked-in behavior descriptions against Python definitions without importing them."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from collections.abc import Iterator

DEFAULT_MAP = Path("evals/codebase_navigability/behavior_map.json")


class MapError(ValueError):
    """A behavior entry cannot be resolved in the nominated checkout."""


def _checkout_file(root: Path, relative: object, label: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative.strip()
        or Path(relative).is_absolute()
    ):
        raise MapError(f"{label}: expected a checkout-relative path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise MapError(f"{label}: path resolves outside checkout: {relative}")
    if not path.is_file():
        raise MapError(f"{label}: file does not exist: {relative}")
    return path


def _definitions(nodes: list[ast.stmt], prefix: str = "") -> Iterator[str]:
    for node in nodes:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + node.name
            yield name
            if isinstance(node, ast.ClassDef):
                yield from _definitions(node.body, name + ".")


def _check_source(
    root: Path, locator: object, label: str, symbols: dict[Path, set[str]]
) -> None:
    if not isinstance(locator, dict):
        raise MapError(f"{label}: expected a source locator")
    path = _checkout_file(root, locator.get("path"), label)
    if path.suffix != ".py":
        raise MapError(
            f"{label}: expected a Python source file: {path.relative_to(root)}"
        )
    symbol = locator.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise MapError(f"{label}: missing symbol in {path.relative_to(root)}")
    if path not in symbols:
        try:
            module = ast.parse(path.read_bytes(), filename=str(path))
        except (SyntaxError, ValueError) as exc:
            raise MapError(
                f"{label}: cannot parse {path.relative_to(root)}: {exc}"
            ) from exc
        symbols[path] = set(_definitions(module.body))
    if symbol not in symbols[path]:
        raise MapError(f"{label}: {path.relative_to(root)} has no definition {symbol}")


def _check_entry(
    root: Path, entry: object, index: int, symbols: dict[Path, set[str]]
) -> str:
    label = f"entries[{index}]"
    if not isinstance(entry, dict):
        raise MapError(f"{label}: expected an object")
    for field in ("id", "behavior"):
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise MapError(f"{label}.{field}: expected a non-empty string")
    label = entry["id"]
    for field in ("sources", "tests"):
        if not isinstance(entry.get(field), list) or not entry[field]:
            raise MapError(f"{label}.{field}: expected at least one locator")
    for locator in entry["sources"]:
        _check_source(root, locator, label, symbols)
    for test in entry["tests"]:
        _checkout_file(root, test, label)
    for field in ("observed", "inferred"):
        values = entry.get(field, [] if field == "inferred" else None)
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise MapError(f"{label}.{field}: expected an array of strings")
    return label


def validate_map(root: Path, map_path: Path) -> int:
    root = root.resolve()
    data = json.loads(map_path.read_bytes())
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise MapError("map: expected at least one entry")
    seen: set[str] = set()
    symbols: dict[Path, set[str]] = {}
    for index, entry in enumerate(entries):
        name = _check_entry(root, entry, index, symbols)
        if name in seen:
            raise MapError(f"duplicate entry id: {name}")
        seen.add(name)
    return len(seen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("validate", help="check paths and definitions")
    command.add_argument("--root", required=True, type=Path)
    command.add_argument("--map", type=Path)
    args = parser.parse_args(argv)
    try:
        count = validate_map(args.root, args.map or args.root / DEFAULT_MAP)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"behavior-map validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"valid behavior map: {count} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
