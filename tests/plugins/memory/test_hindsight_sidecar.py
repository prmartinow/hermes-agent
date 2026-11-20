"""The optional embedded daemon stays in a separate, profile-owned environment."""

import json
import sys
from types import SimpleNamespace

import pytest

from plugins.memory.hindsight.sidecar import SidecarClient, SidecarManager, runtime_python


def test_runtime_must_belong_to_the_current_home(tmp_path):
    home = tmp_path / "home"
    env = home / "environments" / "hindsight-embedded" / "gen-test" / "venv"
    (env / "bin").mkdir(parents=True)
    (env / "pyvenv.cfg").touch()
    python = env / "bin" / "python"
    python.touch()
    config = {"embedded_python": str(python)}

    assert runtime_python(config, home) == python
    assert runtime_python(config, tmp_path / "other-home") is None


def test_manager_sends_profile_config_over_stdin_without_other_secrets(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout='HERMES_HINDSIGHT_RESULT="http://127.0.0.1:8123"\n')

    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    monkeypatch.setattr("plugins.memory.hindsight.sidecar.subprocess.run", run)
    manager = SidecarManager(tmp_path / "python", health_grace=45)

    assert manager.start("profile-a", {"HINDSIGHT_API_LLM_API_KEY": "profile-key"}) == "http://127.0.0.1:8123"
    command, kwargs = calls[0]
    assert "profile-key" not in " ".join(command)
    assert json.loads(kwargs["input"])["config"]["HINDSIGHT_API_LLM_API_KEY"] == "profile-key"
    assert "UNRELATED_SECRET" not in kwargs["env"]
    assert kwargs["env"]["HINDSIGHT_EMBED_PORT_HEALTH_GRACE_TIMEOUT"] == "45.0"


def test_embedded_provider_uses_profile_sidecar(tmp_path, monkeypatch):
    from plugins.memory import hindsight

    env = tmp_path / "environments" / "hindsight-embedded" / "gen-test" / "venv"
    (env / "bin").mkdir(parents=True)
    (env / "pyvenv.cfg").touch()
    python = env / "bin" / "python"
    python.touch()
    config = {"mode": "local_embedded", "embedded_python": str(python), "profile": "profile-a", "llm_provider": "openai",
              "llm_api_key": "profile-key", "llm_model": "test-model"}
    monkeypatch.setattr(hindsight, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hindsight, "_load_config", lambda: config)
    monkeypatch.setattr(hindsight, "_ensure_client_dependency", lambda: None)
    provider = hindsight.HindsightMemoryProvider()
    provider._config = config

    assert provider.is_available() is True
    client = provider._new_embedded_client()
    assert isinstance(client, SidecarClient)
    assert client.profile == "profile-a"
    assert client.config["HINDSIGHT_API_LLM_API_KEY"] == "profile-key"


def test_failed_embedded_install_does_not_publish_configuration(tmp_path, monkeypatch):
    from plugins.memory.hindsight import HindsightMemoryProvider, setup

    selections = iter(["local_embedded", "openai"])
    monkeypatch.setattr(setup, "_select", lambda *args: next(selections))

    def fail(_home):
        raise OSError("runtime install failed")

    monkeypatch.setattr(setup, "_prepare_embedded_runtime", fail)
    writes = []
    monkeypatch.setattr("hermes_cli.config.save_config", lambda config: writes.append(config))
    config = {"memory": {}}

    setup.run_setup(HindsightMemoryProvider(), str(tmp_path), config)
    assert not writes
    assert config == {"memory": {}}
    assert not (tmp_path / ".env").exists()


def test_embedded_install_uses_target_profile_index_without_launch_secrets(tmp_path, monkeypatch):
    from plugins.memory.hindsight import setup
    import hermes_constants

    launch = tmp_path / "launch"
    target = tmp_path / "target"
    launch.mkdir()
    target.mkdir()
    (target / ".env").write_text("PIP_INDEX_URL=https://target.example/simple\n", encoding="utf-8")
    monkeypatch.setattr(hermes_constants, "get_routing_process_hermes_home", lambda: launch)
    monkeypatch.setenv("LAUNCH_ONLY_SECRET", "launch-secret")
    monkeypatch.setenv("PIP_INDEX_URL", "https://launch.example/simple")
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, env=kwargs["env"])
        return SimpleNamespace(stdout="/synthetic/python\n")

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup._prepare_embedded_runtime(str(target)) == "/synthetic/python"
    assert observed["env"]["HERMES_HOME"] == str(target)
    assert observed["env"]["PIP_INDEX_URL"] == "https://target.example/simple"
    assert "LAUNCH_ONLY_SECRET" not in observed["env"]


def test_manager_runs_the_isolated_python_protocol(tmp_path):
    module = tmp_path / "hindsight_embed.py"
    module.write_text(
        "class Manager:\n"
        " def ensure_running(self, config, profile):\n"
        "  assert config == {'setting': 'value'} and profile == 'test-profile'\n"
        "  return True\n"
        " def get_url(self, profile): return 'http://127.0.0.1:8123'\n"
        "def get_embed_manager(): return Manager()\n"
    )
    python = tmp_path / "fake-python"
    python.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(tmp_path)!r})\n"
        "exec(sys.argv[-1], {'__name__': '__main__'})\n"
    )
    python.chmod(0o700)

    assert SidecarManager(python).start("test-profile", {"setting": "value"}) == "http://127.0.0.1:8123"


def test_manager_does_not_import_the_working_directory(tmp_path, monkeypatch):
    import subprocess
    import venv

    runtime = tmp_path / "runtime"
    venv.create(runtime, with_pip=False)
    python = runtime / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    site = subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip()
    from pathlib import Path

    Path(site, "hindsight_embed.py").write_text(
        "class Manager:\n"
        " def ensure_running(self, config, profile): return True\n"
        " def get_url(self, profile): return 'http://127.0.0.1:8123'\n"
        "def get_embed_manager(): return Manager()\n", encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hindsight_embed.py").write_text("raise RuntimeError('untrusted workspace module loaded')\n")
    monkeypatch.chdir(workspace)

    assert SidecarManager(python).start("profile-a", {}) == "http://127.0.0.1:8123"


@pytest.mark.parametrize("url", ["http://localhost:8123@evil.example", "https://localhost:8123",
                                 "http://evil:8123", "http://localhost:not-a-port"])
def test_manager_refuses_nonlocal_daemon_urls(monkeypatch, tmp_path, url):
    manager = SidecarManager(tmp_path / "python")
    monkeypatch.setattr(manager, "_call", lambda *args: url)
    with pytest.raises(RuntimeError, match="invalid local URL"):
        manager.start("profile-a", {})


def test_client_reuses_and_recovers_the_daemon(monkeypatch, tmp_path):
    instances = []

    class Client:
        def __init__(self, base_url):
            self.base_url = base_url
            self.closed = False
            instances.append(self)

        def recall(self):
            return self.base_url

        def close(self):
            self.closed = True

    monkeypatch.setitem(__import__("sys").modules, "hindsight_client", SimpleNamespace(Hindsight=Client))
    client = SidecarClient(tmp_path / "python", "profile-a", {"key": "value"})
    calls = []
    monkeypatch.setattr(client._manager, "start", lambda profile, config: calls.append((profile, config)) or "http://localhost:8123")
    running = iter([True, False])
    monkeypatch.setattr(client._manager, "is_running", lambda profile: next(running))

    assert client.recall() == "http://localhost:8123"
    assert client.recall() == "http://localhost:8123"
    assert client.recall() == "http://localhost:8123"
    assert len(calls) == 2
    assert instances[0].closed
    client.close()
    assert instances[1].closed
