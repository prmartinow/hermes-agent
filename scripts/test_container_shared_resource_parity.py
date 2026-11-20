#!/usr/bin/env python3
"""Compare explicit host/container resource paths without opening their contents.

This is an opt-in operational audit, not an automatically collected pytest test.
Endpoints are probed only when explicitly supplied with --url.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROBE = r"""
import json, os, sys
from pathlib import Path
home, memory, oauth = map(Path, sys.argv[1:])
resources = {"state.db": home / "state.db", "skills": home / "skills", "memory": memory, "oauth": oauth}
result = {}
for name, path in resources.items():
    try:
        stat = path.stat()
    except FileNotFoundError:
        result[name] = None
        continue
    result[name] = {"device": stat.st_dev, "inode": stat.st_ino, "writable": os.access(path, os.W_OK)}
    if name == "skills":
        result[name]["count"] = sum(child.is_dir() and not child.name.startswith(".") for child in path.iterdir())
print(json.dumps(result))
"""


def read_probe(command: list[str]) -> dict:
    completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise ValueError("resource probe returned no resource mapping")
    return result


def compare_resources(container: dict, host: dict) -> bool:
    passed = True
    for resource in ("state.db", "skills", "memory", "oauth"):
        serving, local = container.get(resource), host.get(resource)
        shared = bool(serving and local and all(
            serving.get(key) == local.get(key) and serving.get(key) is not None
            for key in ("device", "inode")
        ))
        if resource == "skills" and shared:
            shared = serving.get("count") is not None and serving.get("count") == local.get("count")
        if resource == "memory" and shared:
            shared = serving.get("writable") is True
        print(f"{resource}: {'PASS: shared' if shared else 'FAIL: missing, divergent, or not writable'}")
        passed = passed and shared
    return passed


def check_endpoint(url: str) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Hermes-Parity-Audit/1.0"})
        with urllib.request.urlopen(request, timeout=2.0) as response:
            passed = 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        passed = False
    print(f"Endpoint: {'PASS' if passed else 'FAIL'}")
    return passed


def absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("resource paths must be absolute")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--hermes-home", type=absolute_path, required=True)
    parser.add_argument("--memory-dir", type=absolute_path, required=True)
    parser.add_argument("--oauth-dir", type=absolute_path, required=True)
    parser.add_argument("--url", action="append", default=[])
    args = parser.parse_args(argv)
    paths = [str(args.hermes_home), str(args.memory_dir), str(args.oauth_dir)]
    try:
        # The normal service identity must be able to access the shared directories;
        # a default-root docker exec would give a false writable result.
        container = read_probe([
            "docker", "exec", "--user", "hermes", args.container,
            "/opt/hermes/.venv/bin/python", "-c", PROBE, *paths,
        ])
        host = read_probe([sys.executable, "-c", PROBE, *paths])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Resource probe failed: {error}", file=sys.stderr)
        return 1
    passed = compare_resources(container, host)
    for url in args.url:
        passed = check_endpoint(url) and passed
    print("Overall parity: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
