"""Command-line entry point for offline trajectory triage."""

import argparse
import json
from .report import iter_reports
import sys


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--input", required=True)
    report.add_argument("--root", required=True)
    args = parser.parse_args()
    try:
        for row in iter_reports(args.input, args.root):
            print(json.dumps(row, sort_keys=True, separators=(",", ":")))
    except (OSError, RuntimeError) as exc:
        print(f"Could not process trajectory file: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
