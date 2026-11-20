"""Check that no source contains leftover merge conflict markers."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_merge_conflict_markers_in_codebase():
    """Verify no leftover git conflict markers exist anywhere in source code."""
    # Matches <<<<<<< branch/commit or >>>>>>> branch/commit
    marker_pattern = re.compile(r"^(<{7}|>{7})\s+\S+", re.MULTILINE)

    extensions = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".yaml", ".yml"}
    excluded_dirs = {".git", "node_modules", "dist", ".cache", "build", "venv", ".venv", ".pytest_cache"}

    # This upstream test intentionally embeds conflict markers in a synthetic fixture.
    marker_fixtures = {"tests/test_audit_old_updater_imports.py"}
    violations = []
    for file_path in REPO_ROOT.rglob("*"):
        if file_path.is_file() and file_path.suffix in extensions:
            if any(part in excluded_dirs for part in file_path.parts):
                continue
            if file_path.relative_to(REPO_ROOT).as_posix() in marker_fixtures:
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
                if marker_pattern.search(text):
                    violations.append(str(file_path.relative_to(REPO_ROOT)))
            except Exception:
                pass

    assert not violations, f"Residual conflict markers found in files: {violations}"
