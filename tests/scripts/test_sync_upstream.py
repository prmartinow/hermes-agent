"""The repository sync entrypoint delegates to the installed GitHub Ops workflow."""
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sync_upstream.sh"


@pytest.mark.parametrize("exit_code", [0, 23])
def test_sync_forwards_checkout_arguments_and_exit_status(tmp_path, exit_code):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    central = tmp_path / "github ci"
    (central / "scripts").mkdir(parents=True)
    receipt = tmp_path / "receipt"
    (central / "scripts/sync_hermes.sh").write_text(
        'printf "%s\\0" "$HERMES_DIR" "$@" > "$SYNC_RECEIPT"\n'
        'exit "$SYNC_EXIT_CODE"\n'
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "--push"], capture_output=True, text=True,
        env=dict(os.environ, HOME=str(tmp_path / "home"), HERMES_AGENT_DIR=str(checkout),
                 GITHUB_CI_DIR=str(central), SYNC_RECEIPT=str(receipt), SYNC_EXIT_CODE=str(exit_code)),
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert receipt.read_bytes().split(b"\0") == [str(checkout).encode(), b"--push", b""]
    assert list(checkout.iterdir()) == []


def test_missing_orchestrator_refuses_without_mutating_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    unsaved = checkout / "unsaved"
    unsaved.write_text("preserve this\n")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"], capture_output=True, text=True,
        env=dict(os.environ, HOME=str(tmp_path / "home"), HERMES_AGENT_DIR=str(checkout),
                 GITHUB_CI_DIR=str(tmp_path / "missing")),
    )
    assert result.returncode != 0
    assert "GITHUB_CI_DIR" in result.stderr
    assert unsaved.read_text() == "preserve this\n"
    assert list(checkout.iterdir()) == [unsaved]
