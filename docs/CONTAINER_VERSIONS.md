# Hermes Agent Container Version Ledger

Authoritative version and build ledger for containerized Hermes Agent deployments.

## Active Deployment (Single Container)

| Container Name | Target Port | Active Image Tag | Compose Configuration | Role |
| :--- | :---: | :--- | :--- | :--- |
| **`hermes-agent-serving`** | `9119` | `hermes-agent:v0.16` (`v0.16`, `local`) | `docker-compose.local.yml` | Active production serving & leader lease holder |

---

## Container Release History

| Version | Image ID | Git Commit | Build Date (UTC) | Core Changes & Fixes | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`v0.17`** | `d7fa1846e1ba` | `b4295a19cc` | 2026-09-03 14:13 | Candidate release v0.17: single container architecture baseline from latest local codebase | Built (Available) |
| **`v0.16`** | `a6bc3a522f48` | `869610dbbe` | 2026-09-03 07:15 | Single container architecture baseline, agy v1.1.24, main-wrapper background spawn guard | **Active (Serving :9119)** |
| **`v0.15`** (Pruned) | `2fd7a9a2d368` | `491b688d0c` | 2026-09-02 20:14 | Rebuilt candidate v0.15: latest fixes, single container workflow, main-wrapper background guard | Pruned |
| **`v0.14`** (Pruned) | `011297896f15` | `7add2d2c88` | 2026-09-02 18:05 | Candidate release v0.14: single container architecture baseline, agy v1.1.24, parity script updates | Pruned |
| **`v0.13`** (`stable`) | `ede551db54fe` | `fb6178e5f1` | 2026-09-02 05:56 | Baked `agy v1.1.24` into image, hermes-test project isolation, web server public hosts fix, unified 1:1 host mount architecture baseline | Available (`stable`) |
| **`v0.12`** (Pruned) | `1181d8414f9a` | `875e13e227` | 2026-09-01 01:43 | Unified 1:1 host mount architecture (`~/.hermes`), read-write memory mount (`~/.hermes/agent-memory:rw`), explicit compose project names | Pruned |
| **`v0.11`** (Pruned) | `2377019b0d6a` | `58a25bffbb` | 2026-08-31 13:51 | Staging candidate deployment, ledger synchronization, v0.9 pruning | Pruned |
| **`v0.10`** (Pruned) | `3f9a62efeb7e` | `ad7e950eb6` | 2026-08-31 12:55 | Vite TS compilerPreset null-check hardening, container release ledger pruning, stability test suite, compose gitignore updates | Pruned |
| **`v0.9`** (Pruned) | `deb795f589d6` | `9043b6c436` | 2026-08-31 08:50 | `HERMES_WRITE_SAFE_ROOT` workspace parity in compose files, tool safety synchronization | Pruned |
| **`v0.8`** (Pruned) | `c4237c792403` | `aa1ccfc63f` | 2026-08-31 03:17 | Container version tracking system, OCI metadata labels, release automation CLI (`scripts/container_release.sh`), and version ledger | Pruned |
| **`v0.7`** (Pruned) | `e983e7890fe0` | `690ec085d2` | 2026-08-31 01:35 | Active-passive background daemon leader election lease, default 15s SQLite `busy_timeout`, parameterized test compose mounts | Pruned |
| **`v0.6`** (Pruned) | `5c3f10892ce1` | `9cef1e7bdd` | 2026-08-30 05:53 | Immutable build-time `agy v1.1.22` packaging, OAuth secondary token file disconnect unlinking | Pruned |
| **`v0.5`** (Pruned) | `20c99f3f1601` | `4bbd6a833a` | 2026-08-30 01:01 | Custom-built SQLite `3.53.4` with trigram FTS5 support | Pruned |
| **`v0.4`** (Pruned) | `d84bbb689cf7` | `ac1ed99c08` | 2026-08-29 12:37 | Dual-slot promotion workflow and container staging verification | Pruned |
| **`v0.3`** (Pruned) | `15ebcd346e1c` | `f0c2c58fea` | 2026-08-29 05:31 | Initial container transition from host systemd service | Pruned |
| **`v0.2`** | `8b291a82f301` | `5698dcde91` | 2026-08-26 10:40 | Dual-port configuration (9119 serving / 9120 test) | Historical |
| **`v0.1`** | `3f9901d892c0` | `35585a0891` | 2026-08-26 03:38 | Initial multi-stage Dockerfile with s6-overlay supervision | Historical |

---

## Container Invariants

1. **Dual-Slot Isolation**:
   - Production serves on port `9119` via `docker-compose.local.yml`.
   - Staging tests on port `9120` via `docker-compose.test.yml`.
   - Never stop or restart the active serving container from inside an active agent session.
2. **File Tool Safe Root Parity**:
   - `HERMES_WRITE_SAFE_ROOT` expands to `/opt/data:${HOST_HOME:-~}:${WORKSPACE_DIR:-/workspace}:/workspace` in compose files so `patch`/`write_file` match `terminal` host scope.
3. **Distributed Leader Election**:
   - Background polling daemons acquire an atomic file lease (`runtime/quota_refresher.lease`).
   - Only the active leader container executes scheduled API polling and writes to `state.db`.
4. **Immutable Packaging**:
   - Static binaries and dependencies are baked directly into Dockerfile layers to prevent `overlay2` copy-on-write disk bloat.
5. **Volume Mount Permissions**:
   - All OAuth credential directories (`~/.gemini`) must be mounted with `:rw` permissions.
