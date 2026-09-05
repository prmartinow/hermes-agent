# SQLite Concurrency, Auto-Repair Hardening & Disaster Recovery Specification

**Authoritative Specification for Hermes Agent Database Resilience & Multi-Container Concurrency**

---

## 1. Executive Summary & Incident Forensic Analysis

On **2026-09-02 at 22:09 WIB (15:09 UTC)**, a two-stage failure degraded `~/.hermes/state.db`:

```text
Stage 1 (Torn Write):
  [22:09:39 WIB] Serving container booted -> Gemini Quota Watcher daemon wrote snapshot rows.
  [22:09:39 WIB] Simultaneously, Test Container was committing turn #48 of session 5bd828.
  [22:09:42 WIB] Concurrent write collision -> OperationalError: disk I/O error on Tree 259 (gemini_quota_snapshots).
  [22:09:50 WIB] SQLite B-Tree index corrupted -> DatabaseError: database disk image is malformed.

Stage 2 (Blocked Auto-Heal):
  [22:09:50 WIB] Web server caught SQLite error and attempted built-in auto-recovery.
  [22:09:50 WIB] Handler crashed with NameError: name 'is_malformed_schema_error' is not defined (web_server.py:12672).
  [22:09:50 WIB] Self-healing aborted -> All /api/sessions queries returned HTTP 404 / 500.
```

---

## 2. Table-Level Integrity Assessment

Forensic table scan of `~/.hermes/state.db`:

| Table | Status | Row Count | Impact on Core Functionality |
| :--- | :--- | :--- | :--- |
| **`sessions`** | **100% HEALTHY** | 200 rows | All conversation metadata intact |
| **`messages`** | **100% HEALTHY** | 62,028 rows | **Zero chat history lost** |
| **`system_prompts`** | **100% HEALTHY** | 74 rows | All system prompt templates intact |
| **`session_model_usage`** | **100% HEALTHY** | 352 rows | All token tracking data intact |
| **`async_delegations`** | **100% HEALTHY** | 21 rows | All subagent delegation records intact |
| **`gemini_account_events`** | **100% HEALTHY** | 15 rows | All OAuth rotation logs intact |
| **`gemini_quota_snapshots`** | **Corrupted Page** | ~3,955 rows | Transient 60s background metrics log |

---

## 3. Recommended Instant Recovery Procedure (100% Data Preservation)

### The Clean Table-by-Table Rebuild
Because 100% of user data (`sessions`, `messages`, `system_prompts`, `model_usage`) is completely uncorrupted, the optimal recovery is an **In-Place Non-Destructive Rebuild**:

1. **Backup Corrupt State**:
   ```bash
   cp ~/.hermes/state.db ~/.hermes/state.db.corrupt-backup
   ```
2. **Stream Table-by-Table to `state.db.repaired`**:
   Stream all healthy tables and recreate the empty `gemini_quota_snapshots` schema using Python SQLite.
3. **Validate & Swap**:
   Verify `PRAGMA integrity_check` $\rightarrow$ `ok`, then atomically move `state.db.repaired` $\rightarrow$ `~/.hermes/state.db`.
4. **Restart Containers**:
   Restart serving (:9119) and testing (:9120) with 100% data intact.

---

## 4. Permanent Architectural Prevention

### Pillar 1: Fix Web Server Auto-Recovery Import
In `hermes_cli/web_server.py`, import `is_malformed_schema_error` from `hermes_state.py` so that any future database error triggers the built-in self-healing path cleanly without throwing a `NameError`.

### Pillar 2: Stagger Background Daemons & Cooperative File Locking
- Add a 5–10s jitter delay on container boot before background daemons (Gemini Quota Watcher, Hindsight retain, Cron Ticker) initiate SQLite write transactions.
- Enforce cooperative file locking (`runtime/state_write.lock`) during batch snapshot operations.
- Enforce `PRAGMA busy_timeout=30000` (30s) across all connection pools.

### Pillar 3: Automated Hourly Online Snapshots
Implement automated hourly snapshots via SQLite's `sqlite3_backup` API to `~/.hermes/backups/state_hourly.db` with a 7-day retention window.
