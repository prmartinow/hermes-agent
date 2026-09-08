"""Tests for nested nsenter prevention and host HERMES_HOME inheritance in LocalEnvironment."""

import os
from unittest.mock import MagicMock, patch

from tools.environments.local import (
    LocalEnvironment,
    _is_already_in_host_namespace,
)


def test_probe_same_namespace():
    """When /proc/self/ns/mnt and /proc/1/ns/mnt have the same inode, probe returns True."""
    mock_stat_self = MagicMock(st_ino=12345)
    mock_stat_init = MagicMock(st_ino=12345)

    def fake_stat(path):
        if path == "/proc/self/ns/mnt":
            return mock_stat_self
        elif path == "/proc/1/ns/mnt":
            return mock_stat_init
        raise FileNotFoundError(path)

    with patch("os.path.exists", return_value=True), patch("os.stat", side_effect=fake_stat):
        assert _is_already_in_host_namespace() is True


def test_probe_different_namespace():
    """When /proc/self/ns/mnt and /proc/1/ns/mnt have different inodes, probe returns False."""
    mock_stat_self = MagicMock(st_ino=12345)
    mock_stat_init = MagicMock(st_ino=67890)

    def fake_stat(path):
        if path == "/proc/self/ns/mnt":
            return mock_stat_self
        elif path == "/proc/1/ns/mnt":
            return mock_stat_init
        raise FileNotFoundError(path)

    with patch("os.path.exists", return_value=True), patch("os.stat", side_effect=fake_stat):
        assert _is_already_in_host_namespace() is False


def test_probe_on_windows():
    """On Windows, probe returns False."""
    with patch("tools.environments.local._IS_WINDOWS", True):
        assert _is_already_in_host_namespace() is False


def test_run_bash_skips_nsenter_when_in_host_namespace():
    """When HERMES_USE_NSENTER=1 but process is in host namespace, nsenter is skipped."""
    env = LocalEnvironment.__new__(LocalEnvironment)
    env.cwd = "/workspace/project"
    env.env = {}

    captured_args = []

    def fake_popen(args, **kwargs):
        captured_args.append(args)
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.stdout = MagicMock(__iter__=lambda s: iter([]))
        proc.stdin = MagicMock()
        return proc

    with patch.dict(os.environ, {"HERMES_USE_NSENTER": "1"}), \
         patch("tools.environments.local._is_already_in_host_namespace", return_value=True), \
         patch("tools.environments.local._find_bash", return_value="/bin/bash"), \
         patch("subprocess.Popen", side_effect=fake_popen):
        env._run_bash("echo 'test direct'")

    assert len(captured_args) == 1
    assert captured_args[0] == ["/bin/bash", "-c", "echo 'test direct'"]


def test_run_bash_uses_nsenter_and_sanitizes_env_prefix_when_in_container():
    """When HERMES_USE_NSENTER=1 and in container, nsenter is used and env_prefix exports HERMES_USE_NSENTER=0 and host HERMES_HOME."""
    env = LocalEnvironment.__new__(LocalEnvironment)
    env.cwd = "/workspace/project"
    env.env = {}

    captured_args = []

    def fake_popen(args, **kwargs):
        captured_args.append(args)
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.stdout = MagicMock(__iter__=lambda s: iter([]))
        proc.stdin = MagicMock()
        return proc

    with patch.dict(os.environ, {
            "HERMES_USE_NSENTER": "1",
            "HERMES_HOST_USER": "testuser",
            "HERMES_UID": "1000",
            "HERMES_GID": "1000",
         }), \
         patch("tools.environments.local._is_already_in_host_namespace", return_value=False), \
         patch("subprocess.Popen", side_effect=fake_popen):
        env._run_bash("echo 'test container'")

    assert len(captured_args) == 1
    args = captured_args[0]
    assert args[0] == "nsenter"
    assert "setpriv" in args
    assert "--reuid" in args
    assert "1000" in args
    wrapped_cmd = args[-1]
    assert "export HERMES_USE_NSENTER=0 HERMES_HOME=/home/testuser/.hermes" in wrapped_cmd
    assert "HOME=/home/testuser" in wrapped_cmd
    assert "echo 'test container'" in wrapped_cmd
