# Multi-Agent Implementation Orchestration Plan: Concurrency-Dampened DOCI (CD-DOCI) & Subagent Allocation Engine

**Document Path**: `docs/plans/cd_doci_orchestration_plan.md`  
**Specification Reference**: `~/.hermes/cache/delegation/phase3_algorithm_research_report.md`  
**Author**: Hermes Multi-Agent Orchestrator  
**Status**: Ready for Execution  

---

## 1. Executive Summary & Graph Architecture

This orchestration plan operationalizes the mathematical and architectural findings from the **Phase 3 Algorithm Research Report**. It eliminates the subagent credential allocation defect, introduces real-time in-memory concurrency lease dampening, implements Power-of-Two-Choices ($d=2$) randomized candidate selection, and guarantees even load distribution across all 5 Gemini accounts while strictly preserving **92.8% KV-cache prompt stickiness**.

### Graph Topology: Directed Acyclic Graph (DAG) with Verification Gates

```
                                  ORCHESTRATION PIPELINE
                                  ══════════════════════

  [ Stage 1: Core Allocation Fix ]
  Node A: Subagent Lease & Cursor Sync
  (agent/credential_pool.py, tools/delegate_tool.py)
                 │
                 ▼
  [ Stage 2: Concurrency & Mathematical Engine ]
  Node B: In-Memory Active Lease Tracker  ──┐
  (agent/credential_pool.py)                ├─► Node D: CD-DOCI Selection Engine
                                            │   (Power-of-Two Choices d=2 in _select_unlocked)
  Node C: Smooth Sigmoid & DOCI Scoring   ──┘
  (hermes_cli/auth.py)
                 │
                 ▼
  [ Stage 3: Verification & Simulation Suite ]
  Node E: Concurrent Burst & Unit Test Suite
  (tests/agent/test_cd_doci_rotation.py)
                 │
                 ▼
   [ Stage 4: Deployment & Container Rollout ]
   Node F: 3-Branch Git Rollout & Live Validation
   (dev -> origin/dev -> local -> origin/local -> container)
```

---

## 2. Node Specifications & Engineering Contracts

### Node A: Subagent Lease & Cursor Synchronization
* **Objective**: Fix the subagent credential binding flaw where `_acquire_lease_under_lock()` omitted `self._current_id` updates, causing all subagents to fall back to the default account (priority 0).
* **Target Files**:
  * `agent/credential_pool.py`:
    * Implement `get_entry(credential_id: str) -> Optional[PooledCredential]`.
    * Update `_acquire_lease_under_lock()` to explicitly assign `self._current_id = chosen.id`.
  * `tools/delegate_tool.py`:
    * In lines 2454–2465, resolve leased credentials directly via `child_pool.get_entry(leased_cred_id)` and invoke `child._swap_credential(leased_entry)`.
* **Output Contract**: Subagents receive explicit leased credentials and never fall back to entry index 0.

---

### Node B: In-Memory Active Session Lease Tracker
* **Objective**: Track live session-to-account bindings in memory to provide real-time concurrency metrics ($L(a)$) without database polling.
* **Target Files**:
  * `agent/credential_pool.py`:
    * Add `_active_session_leases: Dict[str, Set[str]]` (mapping account ID $\to$ set of active `session_id`s).
    * Add thread-safe methods:
      * `acquire_session_lease(session_id: str, account_id: str) -> None`
      * `release_session_lease(session_id: str, account_id: Optional[str] = None) -> None`
      * `get_active_session_count(account_id: str) -> int`
  * `agent/agent_init.py` & `run_agent.py`:
    * Register lease release on session cleanup and completion hooks.
* **Output Contract**: Thread-safe, real-time count of active concurrent sessions per account ($L(a) \ge 0$).

---

### Node C: Enhanced CD-DOCI Mathematical Scoring Engine
* **Objective**: Upgrade `calculate_gemini_doci_score()` with the research paper's mathematical formulation:
  $$\text{Score}_{\text{CD-DOCI}}(a) = \text{DOCI}_{\text{smooth}}(a) \times e^{-0.40 \cdot L(a)}$$
* **Target Files**:
  * `hermes_cli/auth.py`:
    * Add optional `active_leases: int = 0` parameter to `calculate_gemini_doci_score()`.
    * Replace the discrete step-barrier at $20\%$ capacity with a smooth sigmoid barrier:
      $$\text{Barrier}(C_{5h}) = \frac{1}{1 + e^{-\frac{C_{5h} - 0.20}{0.025}}}$$
    * Apply exponential lease dampener $\Phi(L) = e^{-0.40 \cdot L}$.
    * Maintain backward compatibility for timeline snapshots where `active_leases=0`.
* **Output Contract**: Continuous, concurrency-sensitive opportunity cost scores for all 5 accounts.

---

### Node D: Power-of-Two Choices ($d=2$) Candidate Selection
* **Objective**: Replace deterministic greediness with $d=2$ randomized candidate comparison during unassigned session/subagent binding to eliminate the thundering herd.
* **Target Files**:
  * `agent/credential_pool.py`:
    * In `_select_unlocked()` (when `preferred_account` is not pinned):
      1. Filter candidate accounts passing safety gates ($C_{5h} \ge 20\%$, $C_w \ge 10\%$, unexhausted).
      2. If $|\text{candidates}| > 2$: Draw two distinct accounts $(a_1, a_2)$ uniformly at random and select:
         $$a^* = \operatorname{argmax}_{a \in \{a_1, a_2\}} \text{Score}_{\text{CD-DOCI}}(a)$$
      3. If $|\text{candidates}| \le 2$: Select $a^* = \operatorname{argmax}_{a} \text{Score}_{\text{CD-DOCI}}(a)$.
      4. Bind session to $a^*$ and increment lease count $L(a^*)$.
* **Output Contract**: Evenly dispersed burst allocation across candidate accounts with bounded maximum load $O(\frac{\ln \ln n}{\ln 2})$.

---

### Node E: Simulation Test Suite & E2E Validation
* **Objective**: Build a comprehensive automated test suite verifying all mathematical and concurrency invariants.
* **Target Files**:
  * `tests/agent/test_cd_doci_rotation.py`:
    * Test 1: Single-session DOCI score accuracy and smooth sigmoid barrier.
    * Test 2: Subagent lease acquisition directly resolving to leased account (not defaulting to entry 0).
    * Test 3: Concurrent Burst Mock — Simulate 8 parallel subagent allocations arriving within 50ms, asserting load distribution across all available accounts.
    * Test 4: KV-Cache Stickiness — Assert that subsequent turns within a bound session stay locked to the pinned account.
    * Test 5: Session lease release on completion.
* **Output Contract**: 100% green pytest execution via `scripts/run_tests.sh`.

---

### Node F: 3-Branch Git Rollout & Container Deployment
* **Objective**: Commit, merge, deploy, and verify against live container.
* **Execution Sequence**:
  1. Commit on `dev` consolidated topic branch $\to$ push `origin/dev`.
  2. Switch to `local` branch $\to$ merge `dev` ($\text{--no-ff}$) $\to$ push `origin/local`.
  3. Rebuild web/UI assets (`npm run build:ink && npm run build`).
  4. Deploy container release and verify health endpoint.
  5. Playwright CDP visual inspection of `/gemini/timeline`.

---

## 3. Subagent Execution Matrix

| Subagent / Task ID | Assigned Node | Role | Scope | Dependencies |
| :--- | :---: | :---: | :--- | :--- |
| **Worker 1: Core Engine** | Nodes A, B, D | `leaf` | `agent/credential_pool.py`, `tools/delegate_tool.py` | None |
| **Worker 2: Math Scoring** | Node C | `leaf` | `hermes_cli/auth.py` | None |
| **Worker 3: Test Suite** | Node E | `leaf` | `tests/agent/test_cd_doci_rotation.py` | Nodes A, B, C, D |
| **Supervisor (Main)** | Node F & QA | `orchestrator` | Branch merge, `scripts/run_tests.sh`, `systemctl` | All Workers |

---

## 4. Verification & Quality Gates

1. **Unit Test Gate**: `scripts/run_tests.sh tests/agent/test_cd_doci_rotation.py tests/test_gemini_quota_timeline.py` must pass with 0 failures.
2. **Subagent Allocation Gate**: Dispatch a test batch of 3 parallel subagents and verify via `agent.log` that they lease across distinct accounts instead of stacking 100% on a single default.
3. **Cache Stickiness Gate**: Verify that interactive session turns continue to use the pinned session account with zero mid-session drift.
4. **Service Health Gate**: `systemctl --user status hermes-dashboard.service` running with 0 error logs.
