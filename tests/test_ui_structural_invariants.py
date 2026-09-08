"""Static structural invariant test to prevent duplicate JSX elements and UI regressions."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_chat_session_list_no_duplicate_account_alias():
    """Verify ChatSessionList.tsx never has duplicate account_alias blocks."""
    path = REPO_ROOT / "web" / "src" / "components" / "ChatSessionList.tsx"
    assert path.exists(), f"Missing {path}"
    content = path.read_text(encoding="utf-8")
    
    # Count occurrences of s.account_alias in JSX
    alias_matches = re.findall(r"\{s\.account_alias\s*&&", content)
    assert len(alias_matches) <= 1, (
        f"ChatSessionList.tsx contains {len(alias_matches)} account_alias blocks! "
        "It must contain at most 1 to prevent duplicate sidebar badges."
    )


def test_no_merge_conflict_markers_in_codebase():
    """Verify no leftover git conflict markers exist anywhere in source code."""
    # Matches <<<<<<< branch/commit or >>>>>>> branch/commit
    marker_pattern = re.compile(r"^(<{7}|>{7})\s+\S+", re.MULTILINE)
    
    extensions = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".yaml", ".yml"}
    excluded_dirs = {".git", "node_modules", "dist", ".cache", "build", "venv", ".venv", ".pytest_cache"}
    
    violations = []
    for file_path in REPO_ROOT.rglob("*"):
        if file_path.is_file() and file_path.suffix in extensions:
            if any(part in excluded_dirs for part in file_path.parts):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
                if marker_pattern.search(text):
                    violations.append(str(file_path.relative_to(REPO_ROOT)))
            except Exception:
                pass
                
    assert not violations, f"Residual conflict markers found in files: {violations}"
