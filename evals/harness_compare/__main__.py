"""Validate an offline evaluation manifest or report its recorded results."""

import argparse
import json
import sys
from pathlib import Path

from .report import build_report, validate_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline harness manifest validator and result reporter"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--manifest", required=True)
    report = sub.add_parser("report")
    report.add_argument("--manifest", required=True)
    report.add_argument("--results", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(Path(args.manifest).read_bytes())
        task_splits = validate_manifest(manifest)
        if args.command == "validate":
            print("valid")
        else:
            report = build_report(task_splits, args.results)
            report.update(
                model=manifest["model"],
                config=manifest["config"],
                budget={"max_tokens": manifest["budget"]["max_tokens"]},
            )
            print(json.dumps(report, sort_keys=True, allow_nan=False))
    except (OSError, ValueError, OverflowError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
