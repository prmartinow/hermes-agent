import sys

import pytest

from scripts import test_container_shared_resource_parity as parity


@pytest.mark.parametrize(
    ("oauth_mount", "expected_status"),
    [("omitted", 0), ("shared", 0), ("missing", 1)],
)
def test_optional_credential_mount(tmp_path, monkeypatch, capsys, oauth_mount, expected_status):
    (tmp_path / "state.db").touch()
    (tmp_path / "skills").mkdir()
    memory = tmp_path / "memory"
    memory.mkdir()
    args = ["--container", "test", "--hermes-home", str(tmp_path), "--memory-dir", str(memory)]
    if oauth_mount != "omitted":
        credentials = tmp_path / "credentials"
        if oauth_mount == "shared":
            credentials.mkdir()
        args.extend(["--oauth-dir", str(credentials)])

    # Both probes see the same test directory, as they would with a shared bind mount.
    read_probe = parity.read_probe

    def run_local_probe(command):
        return read_probe([sys.executable, *command[command.index("-c"):]])

    monkeypatch.setattr(parity, "read_probe", run_local_probe)
    assert parity.main(args) == expected_status
    assert ("oauth:" in capsys.readouterr().out) == (oauth_mount != "omitted")
