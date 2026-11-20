"""Run the optional embedded daemon in its own PM-managed Python environment."""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

_MANAGER_PROGRAM = """
import json, sys
from hindsight_embed import get_embed_manager
request = json.load(sys.stdin)
manager = get_embed_manager()
profile = request['profile']
action = request['action']
if action == 'start':
    if not manager.ensure_running(request['config'], profile):
        raise RuntimeError('Hindsight daemon failed to start')
    result = manager.get_url(profile)
elif action == 'running':
    result = manager.is_running(profile)
elif action == 'stop':
    result = manager.stop(profile)
else:
    raise ValueError('Unknown Hindsight daemon action')
print('HERMES_HINDSIGHT_RESULT=' + json.dumps(result))
"""
_RESULT_PREFIX = "HERMES_HINDSIGHT_RESULT="


def runtime_python(config: dict, hermes_home: Path) -> Path | None:
    """Accept only a prepared side environment belonging to this Hermes home."""
    raw = config.get("embedded_python")
    if not isinstance(raw, str) or not raw:
        return None
    python = Path(raw).absolute()
    root = (hermes_home / "environments" / "hindsight-embedded").absolute()
    if (not python.is_relative_to(root) or not python.is_file()
            or not python.parent.resolve().is_relative_to(root.resolve())):
        return None
    return python if (python.parent.parent / "pyvenv.cfg").is_file() else None


class SidecarManager:
    def __init__(self, python: Path, health_grace=None):
        self.python = python
        self.health_grace = health_grace

    def _call(self, action: str, profile: str, config: dict | None = None):
        allowed = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR",
                   "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC", "TEMP", "TMP")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        if self.health_grace is not None and self.health_grace != "":
            try:
                seconds = float(self.health_grace)
            except (TypeError, ValueError):
                seconds = -1
            if math.isfinite(seconds) and seconds >= 0:
                env["HINDSIGHT_EMBED_PORT_HEALTH_GRACE_TIMEOUT"] = str(seconds)
        result = subprocess.run(
            [str(self.python), "-I", "-c", _MANAGER_PROGRAM],
            input=json.dumps({"action": action, "profile": profile, "config": config or {}}),
            text=True, capture_output=True, check=True, timeout=240,
            env=env,
        )
        for line in reversed(result.stdout.splitlines()):
            if line.startswith(_RESULT_PREFIX):
                return json.loads(line[len(_RESULT_PREFIX):])
        raise RuntimeError("Hindsight sidecar returned no result")

    def is_running(self, profile: str) -> bool:
        return bool(self._call("running", profile))

    def stop(self, profile: str):
        return self._call("stop", profile)

    def start(self, profile: str, config: dict) -> str:
        url = self._call("start", profile, config)
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            port = parsed.port if parsed is not None else None
        except ValueError as exc:
            raise RuntimeError("Hindsight sidecar returned an invalid local URL") from exc
        if (parsed is None or parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username is not None or parsed.password is not None or not port):
            raise RuntimeError("Hindsight sidecar returned an invalid local URL")
        return url


class SidecarClient:
    """Hindsight client whose server dependencies stay outside Hermes's Python."""

    def __init__(self, python: Path, profile: str, config: dict, health_grace=None):
        self.profile = profile
        self.config = config
        self._manager = SidecarManager(python, health_grace)
        self._client = None
        self._url = ""
        self._lock = threading.Lock()
        self._closed = False

    def _ensure_started(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Hindsight client is closed")
            if self._client is not None and self._manager.is_running(self.profile):
                return
            if self._client is not None:
                self._client.close()
            self._url = self._manager.start(self.profile, self.config)
            from hindsight_client import Hindsight
            self._client = Hindsight(base_url=self._url)

    @property
    def url(self):
        self._ensure_started()
        return self._url

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._ensure_started()
        return getattr(self._client, name)

    def close(self):
        with self._lock:
            self._closed = True
            if self._client is not None:
                self._client.close()
                self._client = None
