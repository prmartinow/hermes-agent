# Hermes Agent Container Stability & Setup Monitoring Playbook

Operational testing workflow to validate real-life functionality of containerized Hermes Agent deployments and detect setup discrepancies.

---

## 1. Automated Validation Probes

### Probe 1: File Safety & Tool Parity (`HERMES_WRITE_SAFE_ROOT`)
Validates that built-in `patch` and `write_file` tools operate across host repository paths while keeping credential files protected:

```bash
python3 -c '
import os
from pathlib import Path
from agent.file_safety import is_write_denied, get_write_denied_error

workspace_paths = [
    "/opt/data/test_state.tmp",
    str(Path.cwd() / "test_repo.tmp"),
    str(Path.home() / "test_workspace.tmp"),
]
for p in workspace_paths:
    assert not is_write_denied(p), f"Write unexpectedly blocked for {p}: {get_write_denied_error(p)}"
print("✓ Workspace write parity PASS")

credential_paths = [
    str(Path.home() / ".ssh" / "id_rsa"),
    str(Path.home() / ".ssh" / "id_ed25519"),
]
for p in credential_paths:
    assert is_write_denied(p), f"Credential path {p} must be blocked by safety denylist"
print("✓ Credential protection PASS")
'
```

### Probe 2: Distributed Leader Lease & SQLite Concurrency
Validates active-passive leader election and multi-process SQLite write absorption:

```bash
python3 -c '
import json, time, sqlite3
from pathlib import Path

lease_file = Path("/opt/data/runtime/quota_refresher.lease")
assert lease_file.exists(), "Lease file missing"
lease = json.loads(lease_file.read_text())
assert lease.get("expires_at", 0) > time.time(), "Leader lease is expired"
print(f"✓ Leader lease active: {lease.get("holder")}")

conn = sqlite3.connect("/opt/data/state.db", timeout=15.0)
timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
assert timeout >= 15000, f"busy_timeout must be >= 15000 (got {timeout})"
assert wal.lower() == "wal", f"journal_mode must be WAL (got {wal})"
print("✓ SQLite concurrency PRAGMAs verified (15s busy_timeout + WAL)")
conn.close()
'
```

### Probe 3: Microservice Loopback Health
Validates all 5 local services over host networking:

```bash
python3 -c '
import urllib.request

endpoints = [
    ("Serving Dashboard (:9119)", "http://127.0.0.1:9119/api/status"),
    ("Staging Dashboard (:9120)", "http://127.0.0.1:9120/api/status"),
    ("Hindsight Memory (:8888)", "http://127.0.0.1:8888/health"),
    ("Local Inference Qwen (:18200)", "http://127.0.0.1:18200/healthz"),
    ("Rebrowser Chromium CDP (:9225)", "http://127.0.0.1:9225/json/version"),
]
for name, url in endpoints:
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-Probe/1.0"})
    with urllib.request.urlopen(req, timeout=2) as res:
        assert res.status in (200, 201), f"{name} returned unexpected status {res.status}"
        print(f"✓ {name} OK (HTTP {res.status})")
'
```

---

## 2. Automated Log Error Scanner

Scans container and application logs for configuration failure signatures:

```bash
python3 -c '
import re, os
from pathlib import Path

signatures = [
    ("outside HERMES_WRITE_SAFE_ROOT", "File tool sandbox misconfiguration"),
    ("ConnectionRefusedError", "Microservice loopback failure"),
    ("sqlite3.OperationalError: database is locked", "SQLite lock contention"),
    ("Permission denied.*docker.sock", "Docker socket GID mismatch"),
    ("Operation not permitted.*nsenter", "nsenter missing setuid permissions"),
]

for log_path in [Path("/opt/data/logs/errors.log"), Path.home() / ".hermes/logs/errors.log"]:
    if not log_path.exists(): continue
    text = log_path.read_text(encoding="utf-8", errors="replace")
    for pattern, desc in signatures:
        hits = len(re.findall(pattern, text))
        print(f"[{log_path.name}] {pattern:<35} → {hits} hit(s) ({desc})")
'
```
