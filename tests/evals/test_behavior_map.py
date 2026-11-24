import json
from pathlib import Path
import subprocess
import sys

import pytest

from evals.codebase_navigability.behavior_map import MapError, validate_map

ROOT = Path(__file__).resolve().parents[2]


def test_shipped_map_and_public_command_validate():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.codebase_navigability.behavior_map",
            "validate",
            "--root",
            str(ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_nonexistent_symbol_reports_path_and_symbol(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "bad",
                        "behavior": "test",
                        "sources": [
                            {"path": "agent/conversation_loop.py", "symbol": "not_here"}
                        ],
                        "tests": ["tests/agent/test_run_agent.py"],
                        "observed": [],
                        "inferred": [],
                    }
                ]
            }
        )
    )
    with pytest.raises(
        MapError, match=r"agent/conversation_loop\.py has no definition not_here"
    ):
        validate_map(ROOT, path)


def test_rejects_source_path_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def known(): pass")
    (root / "test.py").write_text("# test")
    doc = {
        "entries": [
            {
                "id": "bad",
                "behavior": "test",
                "sources": [{"path": "../outside.py", "symbol": "known"}],
                "tests": ["test.py"],
                "observed": [],
                "inferred": [],
            }
        ]
    }
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(doc))
    with pytest.raises(MapError, match="outside checkout"):
        validate_map(root, map_path)


def test_resolves_dotted_class_method_and_checks_test_file(tmp_path):
    (tmp_path / "src.py").write_text("class A:\n    def method(self): pass\n")
    (tmp_path / "test.py").write_text("# test")
    doc = {
        "entries": [
            {
                "id": "x",
                "behavior": "x",
                "sources": [{"path": "src.py", "symbol": "A.method"}],
                "tests": ["test.py"],
                "observed": [],
                "inferred": [],
            }
        ]
    }
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(doc))
    assert validate_map(tmp_path, map_path) == 1
