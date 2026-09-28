"""Synthetic test fixtures for summary usefulness evaluation.

IMPORTANT: All transcripts and summaries in this file are synthetic handcrafted
fixtures designed for deterministic evaluation harness testing. They are NOT
actual model outputs or real user session data.
"""

from __future__ import annotations

from typing import Any, Dict, List

# -------------------------------------------------------------------------
# Synthetic Multi-Turn Case 1: Database Migration & Port/Host Correction
# Contains:
# - Explicit decision: Abandon direct pg_dump pipe due to MTU frame mismatch;
#   adopt chunked batch cursor export with RocksDB checkpointing.
# - Exact identifiers/constraints:
#   * Host: aurora-pg-prod.vpc-east.internal
#   * Port: 5439
#   * User/role: migrator_worker_v2
#   * Database: billing_ledger_prod
#   * Table: accounts_v2
#   * Policy: SEC-9941
#   * Constraint: Never use --no-owner
#   * SSL mode: verify-full
#   * CA Cert: /etc/ssl/certs/internal-root-ca-2026.crt
#   * Staging dir: /var/scratch/batch_staging/
#   * RocksDB path: /var/data/migration_checkpoint.db
#   * Batch size: 500
#   * Target row count: 1,420,500
#   * Checksum: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
# - Corrected / superseded facts:
#   * Old host: pg-legacy-01.internal (SUPERSEDED / DO NOT TOUCH)
#   * Old port: 5432 (SUPERSEDED)
#   * Old user: pg_legacy_admin (SUPERSEDED)
# - Tool result facts:
#   * Error: MTU socket error 104 on br-prod-0 in pg_dump pipeline
#   * Checksum output: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
#   * Row count verified: 1,420,500
# - Failed approach:
#   * Direct pg_dump | psql pipe streaming failed due to MTU packet frame mismatch
# - Unfinished task:
#   * Verify /var/scratch/batch_staging/ has >= 15GB free, extract first 100,000
#     records from accounts_v2, hold index creation until verification passes.
# -------------------------------------------------------------------------

SYNTHETIC_MIGRATION_TRANSCRIPT: List[Dict[str, Any]] = [
    {
        "role": "system",
        "content": "You are an operations and migration agent for data platform services."
    },
    {
        "role": "user",
        "content": (
            "We need to migrate our account records. Initially we planned to use the "
            "legacy database cluster at `pg-legacy-01.internal` on port 5432 with user `pg_legacy_admin`."
        )
    },
    {
        "role": "assistant",
        "content": "I will probe the connection to pg-legacy-01.internal:5432.",
        "tool_calls": [
            {
                "id": "call_probe_legacy",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "nc -zv pg-legacy-01.internal 5432"}'
                }
            }
        ]
    },
    {
        "role": "tool",
        "tool_call_id": "call_probe_legacy",
        "content": "Connection to pg-legacy-01.internal 5432 port [tcp/postgresql] succeeded!"
    },
    {
        "role": "user",
        "content": (
            "WAIT! Stop. Do not touch `pg-legacy-01.internal:5432` or `pg_legacy_admin`! "
            "That cluster is frozen and decommissioning. We have migrated to active target host "
            "`aurora-pg-prod.vpc-east.internal` on port `5439`. The new connection role is "
            "`migrator_worker_v2`, and the target database is `billing_ledger_prod`. Also, under "
            "security policy SEC-9941, you MUST NEVER use the `--no-owner` flag and MUST set SSL "
            "mode to `verify-full` using CA certificate `/etc/ssl/certs/internal-root-ca-2026.crt`."
        )
    },
    {
        "role": "assistant",
        "content": "Understood. Switching to aurora-pg-prod.vpc-east.internal:5439 with role migrator_worker_v2. I will attempt direct pipe migration using pg_dump | psql for table accounts_v2.",
        "tool_calls": [
            {
                "id": "call_pipe_stream",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "pg_dump -h aurora-pg-prod.vpc-east.internal -p 5439 -U migrator_worker_v2 -d billing_ledger_prod --table=accounts_v2 | psql"}'
                }
            }
        ]
    },
    {
        "role": "tool",
        "tool_call_id": "call_pipe_stream",
        "content": "ERROR: MTU packet frame mismatch on bridge br-prod-0 (mtu=1420 vs 9000). Connection reset by peer: socket write error 104 in pg_dump pipeline. FATAL: streaming pipe aborted after 124,000 bytes. Transaction rollback executed."
    },
    {
        "role": "assistant",
        "content": (
            "The direct `pg_dump | psql` pipe failed due to MTU network fragmentation (socket error 104 on br-prod-0).\n"
            "Key Decision: Abandon network pipe streaming (`pg_dump | psql`). Instead, use chunked batch cursor export with batch_size=500 and write temporary Parquet files to `/var/scratch/batch_staging/` using local RocksDB checkpointing (`/var/data/migration_checkpoint.db`) to ensure restartability without hitting network socket limits."
        ),
        "tool_calls": [
            {
                "id": "call_init_checkpoint",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "python3 -c \\"import hashlib; print(\'INIT_CHECKPOINT:\', hashlib.sha256(b\'migration_init\').hexdigest())\\""}'
                }
            }
        ]
    },
    {
        "role": "tool",
        "tool_call_id": "call_init_checkpoint",
        "content": "INIT_CHECKPOINT: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\nTotal row count to migrate: 1,420,500 records verified."
    },
    {
        "role": "user",
        "content": (
            "The decision to use chunked batch cursor export with RocksDB checkpointing is approved.\n"
            "Now, please execute phase 1 of the migration for table `accounts_v2`: verify that the local "
            "staging dir `/var/scratch/batch_staging/` has at least 15GB free, then begin extracting the "
            "first 100,000 records from `aurora-pg-prod.vpc-east.internal:5439` database `billing_ledger_prod`, "
            "and do NOT start index creation until all batches pass row verification against checksum "
            "`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`."
        )
    }
]

# -------------------------------------------------------------------------
# Synthetic Multi-Turn Case 1 (Expanded): 4k-8k Token Bounded Evaluation Fixture
# Preserves the exact same ground-truth facts, constraints, and decisions as Case 1,
# but includes realistic database inspection, DDL definitions, network traces,
# and verbose dump logs to reach realistic production compaction scale (~5.6k prompt tokens).
# -------------------------------------------------------------------------

SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED: List[Dict[str, Any]] = [
    {
        "role": "system",
        "content": "You are an operations and migration agent for data platform services."
    },
    {
        "role": "user",
        "content": (
            "We need to migrate our account records. Initially we planned to use the "
            "legacy database cluster at `pg-legacy-01.internal` on port 5432 with user `pg_legacy_admin`."
        )
    },
    {
        "role": "assistant",
        "content": "I will probe the connection to pg-legacy-01.internal:5432 and inspect cluster and schema status.",
        "tool_calls": [
            {
                "id": "call_probe_legacy_expanded",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "nc -zv pg-legacy-01.internal 5432 && psql -h pg-legacy-01.internal -p 5432 -U pg_legacy_admin -d billing_ledger_prod -c \'\\d+ accounts_v2\'"}'
                }
            }
        ]
    },
    {
        "role": "tool",
        "tool_call_id": "call_probe_legacy_expanded",
        "content": (
            "Connection to pg-legacy-01.internal 5432 port [tcp/postgresql] succeeded!\n"
            "Network trace to pg-legacy-01.internal (198.51.100.10):\n"
            "  1  gw-vpc-east.internal (198.51.100.1)  0.182 ms  0.155 ms  0.142 ms\n"
            "  2  pg-legacy-01.internal (198.51.100.10)  0.285 ms  0.271 ms  0.264 ms\n"
            "PostgreSQL cluster status:\n"
            "  Version: PostgreSQL 14.9 on x86_64-pc-linux-gnu\n"
            "  Cluster State: READ-ONLY (Decommissioning in progress - scheduled shutdown in 7 days)\n"
            "  Primary Database: billing_ledger_prod\n"
            "  Current User: pg_legacy_admin\n"
            "Table catalog inspect for accounts_v2 on pg-legacy-01.internal:5432:\n"
            "CREATE TABLE public.accounts_v2 (\n"
            "    account_id uuid NOT NULL DEFAULT gen_random_uuid(),\n"
            "    customer_id character varying(64) NOT NULL,\n"
            "    account_type character varying(32) NOT NULL,\n"
            "    status character varying(24) NOT NULL DEFAULT 'active'::character varying,\n"
            "    currency character(3) NOT NULL DEFAULT 'USD'::bpchar,\n"
            "    balance numeric(18,4) NOT NULL DEFAULT 0.0000,\n"
            "    available_balance numeric(18,4) NOT NULL DEFAULT 0.0000,\n"
            "    hold_balance numeric(18,4) NOT NULL DEFAULT 0.0000,\n"
            "    credit_limit numeric(18,4) NOT NULL DEFAULT 0.0000,\n"
            "    interest_rate numeric(6,4) NOT NULL DEFAULT 0.0000,\n"
            "    created_at timestamp with time zone NOT NULL DEFAULT clock_timestamp(),\n"
            "    updated_at timestamp with time zone NOT NULL DEFAULT clock_timestamp(),\n"
            "    closed_at timestamp with time zone,\n"
            "    routing_number character varying(32),\n"
            "    account_number_encrypted bytea,\n"
            "    tier_level integer NOT NULL DEFAULT 1,\n"
            "    is_frozen boolean NOT NULL DEFAULT false,\n"
            "    risk_score smallint NOT NULL DEFAULT 0,\n"
            "    tags text[] DEFAULT '{}'::text[],\n"
            "    metadata jsonb DEFAULT '{}'::jsonb,\n"
            "    version_nonce bigint NOT NULL DEFAULT 1,\n"
            "    CONSTRAINT pk_accounts_v2 PRIMARY KEY (account_id),\n"
            "    CONSTRAINT chk_accounts_v2_status CHECK (status IN ('active', 'suspended', 'closed', 'pending_kyc')),\n"
            "    CONSTRAINT chk_accounts_v2_currency CHECK (currency IN ('USD', 'EUR', 'GBP', 'JPY', 'CAD', 'CHF', 'AUD')),\n"
            "    CONSTRAINT chk_accounts_v2_balance CHECK (balance >= -100000.0000)\n"
            ");\n"
            "CREATE INDEX idx_accounts_v2_cust_id ON public.accounts_v2 USING btree (customer_id);\n"
            "CREATE INDEX idx_accounts_v2_status ON public.accounts_v2 USING btree (status);\n"
            "CREATE INDEX idx_accounts_v2_currency ON public.accounts_v2 USING btree (currency);\n"
            "CREATE INDEX idx_accounts_v2_created_at ON public.accounts_v2 USING btree (created_at DESC);\n"
            "CREATE INDEX idx_accounts_v2_metadata_gin ON public.accounts_v2 USING gin (metadata);\n"
            "Relation summary: 1,420,500 total estimated tuples, 682 MB data size, 314 MB index size.\n"
        ) * 2
    },
    {
        "role": "user",
        "content": (
            "WAIT! Stop. Do not touch `pg-legacy-01.internal:5432` or `pg_legacy_admin`! "
            "That cluster is frozen and decommissioning. We have migrated to active target host "
            "`aurora-pg-prod.vpc-east.internal` on port `5439`. The new connection role is "
            "`migrator_worker_v2`, and the target database is `billing_ledger_prod`. Also, under "
            "security policy SEC-9941, you MUST NEVER use the `--no-owner` flag and MUST set SSL "
            "mode to `verify-full` using CA certificate `/etc/ssl/certs/internal-root-ca-2026.crt`."
        )
    },
    {
        "role": "assistant",
        "content": "Understood. Switching to aurora-pg-prod.vpc-east.internal:5439 with role migrator_worker_v2. I will attempt direct pipe migration using pg_dump | psql for table accounts_v2.",
        "tool_calls": [
            {
                "id": "call_pipe_stream_expanded",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "pg_dump -h aurora-pg-prod.vpc-east.internal -p 5439 -U migrator_worker_v2 -d billing_ledger_prod --table=accounts_v2 --sslmode=verify-full --sslrootcert=/etc/ssl/certs/internal-root-ca-2026.crt | psql -h aurora-pg-prod.vpc-east.internal -p 5439 -U migrator_worker_v2 -d billing_ledger_prod"}'
                }
            }
        ]
    },
    {
        "role": "tool",
        "tool_call_id": "call_pipe_stream_expanded",
        "content": (
            "pg_dump: [archiver (db)] connection to database \"billing_ledger_prod\" on host \"aurora-pg-prod.vpc-east.internal\" port 5439\n"
            "pg_dump: SSL connection (protocol: TLSv1.3, cipher: TLS_AES_256_GCM_SHA384, bits: 256, compression: off)\n"
            "pg_dump: CA certificate: /etc/ssl/certs/internal-root-ca-2026.crt verified with verify-full\n"
            "pg_dump: user: migrator_worker_v2 authenticated\n"
            "pg_dump: saving search_path = ''\n"
            "pg_dump: reading schemas\n"
            "pg_dump: reading user-defined tables\n"
            "pg_dump: reading column info for interesting tables\n"
            "pg_dump: saving database definition\n"
            "pg_dump: dumping contents of table public.accounts_v2\n"
            "pg_dump: processing data for table \"public.accounts_v2\"\n"
            "pg_dump: streaming data block 0: 65536 bytes\n"
            "pg_dump: streaming data block 1: 58464 bytes\n"
            "psql: incoming stream buffer active, transaction open\n"
            "ERROR: MTU packet frame mismatch on bridge br-prod-0 (mtu=1420 vs 9000). Connection reset by peer: socket write error 104 in pg_dump pipeline. FATAL: streaming pipe aborted after 124,000 bytes. Transaction rollback executed.\n"
            "Network diagnostics on failure:\n"
            "  Bridge br-prod-0 configured MTU: 1420\n"
            "  VPC interface ens5 configured MTU: 9000 (jumbo frame mismatch)\n"
            "  TCP MSS negotiated: 8960 -> packet dropped on bridge egress\n"
            "  Kernel: [412948.102914] br-prod-0: packet too big for DF bit (len 8960, mtu 1420)\n"
            "  Socket state: ECONNRESET (104), pipe broken.\n"
        ) * 2
    },
    {
        "role": "assistant",
        "content": (
            "The direct `pg_dump | psql` pipe failed due to MTU network fragmentation (socket error 104 on br-prod-0).\n"
            "Key Decision: Abandon network pipe streaming (`pg_dump | psql`). Instead, use chunked batch cursor export with batch_size=500 and write temporary Parquet files to `/var/scratch/batch_staging/` using local RocksDB checkpointing (`/var/data/migration_checkpoint.db`) to ensure restartability without hitting network socket limits."
        ),
        "tool_calls": [
            {
                "id": "call_init_checkpoint_expanded",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "python3 -c \\"import hashlib; print(\'INIT_CHECKPOINT:\', hashlib.sha256(b\'migration_init\').hexdigest())\\" && df -h /var/scratch/batch_staging/"}'
                }
            }
        ]
    },
    {
        "role": "tool",
        "tool_call_id": "call_init_checkpoint_expanded",
        "content": (
            "INIT_CHECKPOINT: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
            "Total row count to migrate: 1,420,500 records verified.\n"
            "Verification query details:\n"
            "  Query: SELECT count(*), min(account_id), max(account_id) FROM public.accounts_v2;\n"
            "  Result count: 1,420,500\n"
            "  Min UUID: 000018f2-8c9a-4e2b-bb51-789a012d34ef\n"
            "  Max UUID: ffffbc41-3210-48a1-90ee-112233445566\n"
            "Staging environment audit:\n"
            "  Staging path: /var/scratch/batch_staging/\n"
            "  Filesystem: /dev/nvme0n1p3 mounted on /var/scratch (ext4, rw, relatime)\n"
            "  Disk total: 500.0 GB, Used: 214.3 GB, Available: 260.2 GB (free > 15GB required)\n"
            "  Write permissions: drwxr-xr-x 2 migrator migrator 4096 Sep 27 10:14 /var/scratch/batch_staging/\n"
            "RocksDB checkpoint initialization:\n"
            "  Checkpoint path: /var/data/migration_checkpoint.db\n"
            "  Engine: RocksDB v8.10.0 C++ bindings\n"
            "  Options: create_if_missing=true, max_open_files=1000, write_buffer_size=67108864\n"
            "  Column families initialized: default, batches_pending, batches_completed, row_hashes\n"
            "  Status: OK. Ready for chunked batch cursor export with batch_size=500.\n"
        ) * 2
    },
    {
        "role": "user",
        "content": (
            "The decision to use chunked batch cursor export with RocksDB checkpointing is approved.\n"
            "Now, please execute phase 1 of the migration for table `accounts_v2`: verify that the local "
            "staging dir `/var/scratch/batch_staging/` has at least 15GB free, then begin extracting the "
            "first 100,000 records from `aurora-pg-prod.vpc-east.internal:5439` database `billing_ledger_prod`, "
            "and do NOT start index creation until all batches pass row verification against checksum "
            "`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`."
        )
    }
]

# Ground-truth specification for testing the rubric
MIGRATION_CASE_GROUND_TRUTH: Dict[str, Any] = {
    "case_id": "synthetic_migration_case_v1",
    "description": "Database migration with host/port correction, MTU pipe failure, RocksDB decision, and unfinished extraction",
    # Exact required active facts that MUST be retained
    "required_active_facts": [
        "aurora-pg-prod.vpc-east.internal",
        "5439",
        "migrator_worker_v2",
        "billing_ledger_prod",
        "accounts_v2",
        "SEC-9941",
        "--no-owner",  # specifically the constraint forbidding it
        "verify-full",
        "/etc/ssl/certs/internal-root-ca-2026.crt",
        "/var/scratch/batch_staging/",
        "/var/data/migration_checkpoint.db",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "1,420,500",
    ],
    # Facts that were corrected / superseded. If the summary presents them as active/target, it is a contradiction!
    "superseded_facts": [
        "pg-legacy-01.internal",
        "5432",
        "pg_legacy_admin",
    ],
    # Failed approaches that must be remembered so the agent does not retry them
    "failed_approaches": [
        "pg_dump | psql",  # direct streaming pipe failed with MTU socket error 104
    ],
    # Architectural / technical decisions made
    "key_decisions": [
        "RocksDB checkpointing",
        "chunked batch cursor",
    ],
    # Unfinished tasks that must be captured as pending/active intent
    "unfinished_tasks": [
        "verify /var/scratch/batch_staging/ has at least 15GB free",
        "extract first 100,000 records from accounts_v2",
        "hold index creation until row verification passes",
    ]
}

# -------------------------------------------------------------------------
# Handcrafted Summaries for Scorer Discrimination Testing
# Labeled explicitly as SYNTHETIC test inputs, not actual model outputs.
# -------------------------------------------------------------------------

SYNTHETIC_HANDCRAFTED_GOOD_SUMMARY = """[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.
## Historical User Task / Unfinished Intent
User asked: 'Now, please execute phase 1 of the migration for table `accounts_v2`: verify that the local staging dir `/var/scratch/batch_staging/` has at least 15GB free, then begin extracting the first 100,000 records from `aurora-pg-prod.vpc-east.internal:5439` database `billing_ledger_prod`, and do NOT start index creation until all batches pass row verification against checksum `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.'

## Goal
Safely migrate 1,420,500 records from table `accounts_v2` in PostgreSQL database `billing_ledger_prod` at `aurora-pg-prod.vpc-east.internal:5439` to the new cluster using chunked batch cursor extraction and RocksDB local checkpointing.

## Constraints & Preferences
- SUPERSEDED / DO NOT USE: `pg-legacy-01.internal` on port 5432 with `pg_legacy_admin` is frozen and decommissioned.
- ACTIVE TARGET: `aurora-pg-prod.vpc-east.internal` on port 5439, user `migrator_worker_v2`, database `billing_ledger_prod`.
- Security policy SEC-9941: NEVER use the `--no-owner` flag.
- SSL requirement: SSL mode `verify-full` with CA certificate `/etc/ssl/certs/internal-root-ca-2026.crt`.
- Storage: Staging directory `/var/scratch/batch_staging/` must have at least 15GB free.
- Indexing constraint: Do NOT start index creation until all batches pass row verification against checksum `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

## Completed Actions
1. PROBE pg-legacy-01.internal:5432 — connection succeeded, but user issued stop/correction: host is deprecated [tool: terminal]
2. ATTEMPT pg_dump | psql pipeline on aurora-pg-prod.vpc-east.internal:5439 — FAILED with MTU packet mismatch socket error 104 [tool: terminal]
3. VERIFY total records and init checkpoint — verified 1,420,500 records, checksum e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 [tool: terminal]

## Active State
- Checkpoint database: `/var/data/migration_checkpoint.db` initialized.
- Target verified: 1,420,500 records ready for extraction.
- Pending task: Phase 1 extraction (check 15GB free in `/var/scratch/batch_staging/`, extract first 100,000 records).

## Blocked
- Direct network pipe streaming `pg_dump | psql` is blocked/abandoned due to bridge MTU limits (br-prod-0 mtu=1420 vs 9000, socket error 104).

## Key Decisions
- Abandoned direct `pg_dump | psql` network streaming due to bridge MTU socket error 104.
- Adopted chunked batch cursor export (batch_size=500) into Parquet files with local RocksDB checkpointing at `/var/data/migration_checkpoint.db` for crash-safe resumption.

## Errors & Fixes
- User correction: Initial prompt mentioned legacy host pg-legacy-01.internal:5432; user corrected to aurora-pg-prod.vpc-east.internal:5439 with role migrator_worker_v2.
- Network error: MTU frame mismatch on br-prod-0 broke pg_dump pipe. Fixed by switching design to chunked batch cursor export.

## Relevant Files
- `/etc/ssl/certs/internal-root-ca-2026.crt` — SSL root CA
- `/var/data/migration_checkpoint.db` — RocksDB state store
- `/var/scratch/batch_staging/` — Temporary batch dump directory

## Critical Context
- Target database: `billing_ledger_prod`
- Target table: `accounts_v2`
- Total rows: 1,420,500
- Verification checksum: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
"""

# Handcrafted BAD summary #1: Reasserts superseded facts, hallucinates completion, loses constraints
SYNTHETIC_HANDCRAFTED_BAD_CONTRADICTORY_SUMMARY = """[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.
## Historical User Task / Unfinished Intent
None. Everything was completed.

## Goal
Migrate records from database.

## Constraints & Preferences
- Connect to database server at pg-legacy-01.internal on port 5432 using user pg_legacy_admin.
- Use default pg_dump pipeline.

## Completed Actions
1. Connected to pg-legacy-01.internal:5432.
2. Migrated all accounts using pg_dump | psql pipe.
3. Created indexes on accounts table.

## Active State
Migration finished. Ready for next project.

## Blocked
None.

## Key Decisions
Use pg_dump direct pipe for speed.

## Relevant Files
None.
"""

# Handcrafted BAD summary #2: Extremely high shrink ratio, vague fluff, zero exact facts/identifiers
SYNTHETIC_HANDCRAFTED_BAD_VAGUE_SUMMARY = """[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.
## Historical User Task / Unfinished Intent
User asked to do some database work.

## Goal
Perform data operations.

## Constraints & Preferences
Follow security policies and preferences discussed.

## Completed Actions
1. Ran some terminal commands to test connection.
2. Discussed alternative approaches with user.

## Active State
Working on the database.

## Blocked
Hit some network issues earlier.

## Key Decisions
Decided to use a better tool.
"""


# -------------------------------------------------------------------------
# Synthetic Multi-Turn Case 1 (Longer Realistic Multi-Turn Fixture):
# Designed for realistic compaction scale (29 turns, meaningful compressible middle)
# under UNCHANGED production window and tail settings (protect_first_n=3, protect_last_n=20).
#
# Selected compression window under production ContextCompressor:
# - Head: turns 0-3 (system + CLI tool check)
# - Compressible Middle: turns 4-20 (17 turns) spanning:
#   * Corrections: decommissioned pg-legacy-01:5432 -> active target aurora-pg-prod:5439,
#     role migrator_worker_v2, db billing_ledger_prod, policy SEC-9941, verify-full SSL
#   * Failed approach: direct pg_dump | psql pipe broken by MTU packet mismatch (socket error 104)
#   * Key decisions: abandon direct pipe streaming, adopt chunked batch cursor export with RocksDB
#   * Tool results: RocksDB checkpoint sha256 checksum, row count 1,420,500, staging disk check
#   * Unfinished instructions: extract first 100,000 records, verify >=15GB free, hold index creation
# - Protected Tail: turns 21-28 (subsequent parallel batch execution, status checks, memory telemetry)
#
# Actual summary prompt token count: ~3,100 tokens (<= 8,000 token limit).
# -------------------------------------------------------------------------

SYNTHETIC_MIGRATION_TRANSCRIPT_LONGER: List[Dict[str, Any]] = [
    {
        "role": "system",
        "content": "You are an operations and migration agent for data platform services.",
    },
    {
        "role": "user",
        "content": "Initialize migration environment, verify CLI tooling versions and database connectivity status.",
    },
    {
        "role": "assistant",
        "content": "Checking tooling versions.",
        "tool_calls": [
            {
                "id": "call_tool_check_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "pg_dump --version && psql --version"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_tool_check_longer",
        "content": "pg_dump (PostgreSQL) 14.9, psql (PostgreSQL) 14.9",
    },
    # Compressible Middle starts at index 4
    {
        "role": "user",
        "content": (
            "We need to migrate our account records. Initially we planned to use the "
            "legacy database cluster at `pg-legacy-01.internal` on port 5432 with user `pg_legacy_admin`."
        ),
    },
    {
        "role": "assistant",
        "content": "I will probe the connection to pg-legacy-01.internal:5432 and inspect cluster and schema status.",
        "tool_calls": [
            {
                "id": "call_probe_legacy_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "nc -zv pg-legacy-01.internal 5432"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_probe_legacy_longer",
        "content": (
            "Connection to pg-legacy-01.internal 5432 port [tcp/postgresql] succeeded!\n"
            "PostgreSQL cluster status:\n"
            "  Version: PostgreSQL 14.9 on x86_64-pc-linux-gnu\n"
            "  Cluster State: READ-ONLY (Decommissioning in progress - scheduled shutdown in 7 days)\n"
            "  Primary Database: billing_ledger_prod\n"
            "  Current User: pg_legacy_admin\n"
            "Table catalog inspect for accounts_v2 on pg-legacy-01.internal:5432:\n"
            "Relation summary: 1,420,500 total estimated tuples, 682 MB data size, 314 MB index size."
        ),
    },
    {
        "role": "user",
        "content": (
            "WAIT! Stop. Do not touch `pg-legacy-01.internal:5432` or `pg_legacy_admin`! "
            "That cluster is frozen and decommissioning. We have migrated to active target host "
            "`aurora-pg-prod.vpc-east.internal` on port `5439`. The new connection role is "
            "`migrator_worker_v2`, and the target database is `billing_ledger_prod`. Also, under "
            "security policy SEC-9941, you MUST NEVER use the `--no-owner` flag and MUST set SSL "
            "mode to `verify-full` using CA certificate `/etc/ssl/certs/internal-root-ca-2026.crt`."
        ),
    },
    {
        "role": "assistant",
        "content": "Understood. Switching to aurora-pg-prod.vpc-east.internal:5439 with role migrator_worker_v2. I will attempt direct pipe migration using pg_dump | psql for table accounts_v2.",
        "tool_calls": [
            {
                "id": "call_pipe_stream_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "pg_dump -h aurora-pg-prod.vpc-east.internal -p 5439 -U migrator_worker_v2 -d billing_ledger_prod --table=accounts_v2 --sslmode=verify-full --sslrootcert=/etc/ssl/certs/internal-root-ca-2026.crt | psql -h aurora-pg-prod.vpc-east.internal -p 5439 -U migrator_worker_v2 -d billing_ledger_prod"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_pipe_stream_longer",
        "content": (
            "pg_dump: [archiver (db)] connection to database \"billing_ledger_prod\" on host \"aurora-pg-prod.vpc-east.internal\" port 5439\n"
            "pg_dump: SSL connection (protocol: TLSv1.3, cipher: TLS_AES_256_GCM_SHA384, bits: 256, compression: off)\n"
            "pg_dump: CA certificate: /etc/ssl/certs/internal-root-ca-2026.crt verified with verify-full\n"
            "pg_dump: user: migrator_worker_v2 authenticated\n"
            "pg_dump: saving search_path = \"\"\n"
            "pg_dump: reading schemas\n"
            "pg_dump: reading user-defined tables\n"
            "pg_dump: dumping contents of table public.accounts_v2\n"
            "pg_dump: streaming data block 0: 65536 bytes\n"
            "ERROR: MTU packet frame mismatch on bridge br-prod-0 (mtu=1420 vs 9000). Connection reset by peer: socket write error 104 in pg_dump pipeline. FATAL: streaming pipe aborted after 124,000 bytes. Transaction rollback executed.\n"
            "Network diagnostics on failure:\n"
            "  Bridge br-prod-0 configured MTU: 1420\n"
            "  VPC interface ens5 configured MTU: 9000 (jumbo frame mismatch)\n"
            "  TCP MSS negotiated: 8960 -> packet dropped on bridge egress\n"
            "  Kernel: [412948.102914] br-prod-0: packet too big for DF bit (len 8960, mtu 1420)\n"
            "  Socket state: ECONNRESET (104), pipe broken."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "The direct `pg_dump | psql` pipe failed due to MTU network fragmentation (socket error 104 on br-prod-0).\n"
            "Key Decision: Abandon network pipe streaming (`pg_dump | psql`). Instead, use chunked batch cursor export with batch_size=500 and write temporary Parquet files to `/var/scratch/batch_staging/` using local RocksDB checkpointing (`/var/data/migration_checkpoint.db`) to ensure restartability without hitting network socket limits."
        ),
        "tool_calls": [
            {
                "id": "call_init_checkpoint_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "python3 -c \\"import hashlib; print(\'INIT_CHECKPOINT:\', hashlib.sha256(b\'migration_init\').hexdigest())\\" && df -h /var/scratch/batch_staging/"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_init_checkpoint_longer",
        "content": (
            "INIT_CHECKPOINT: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
            "Total row count to migrate: 1,420,500 records verified.\n"
            "Staging environment audit:\n"
            "  Staging path: /var/scratch/batch_staging/\n"
            "  Filesystem: /dev/nvme0n1p3 mounted on /var/scratch (ext4, rw, relatime)\n"
            "  Disk total: 500.0 GB, Used: 214.3 GB, Available: 260.2 GB (free > 15GB required)\n"
            "RocksDB checkpoint initialization:\n"
            "  Checkpoint path: /var/data/migration_checkpoint.db\n"
            "  Status: OK. Ready for chunked batch cursor export with batch_size=500."
        ),
    },
    {
        "role": "user",
        "content": (
            "The decision to use chunked batch cursor export with RocksDB checkpointing is approved.\n"
            "Now, please execute phase 1 of the migration for table `accounts_v2`: verify that the local "
            "staging dir `/var/scratch/batch_staging/` has at least 15GB free, then begin extracting the "
            "first 100,000 records from `aurora-pg-prod.vpc-east.internal:5439` database `billing_ledger_prod`, "
            "and do NOT start index creation until all batches pass row verification against checksum "
            "`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`."
        ),
    },
    {
        "role": "assistant",
        "content": "Beginning phase 1 execution: validating disk space in /var/scratch/batch_staging/.",
        "tool_calls": [
            {
                "id": "call_check_space_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "df -m /var/scratch/batch_staging/"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_check_space_longer",
        "content": (
            "Filesystem 1M-blocks Used Available Use% Mounted on\n"
            "/dev/nvme0n1p3 512000 219443 266400 46% /var/scratch\n"
            "Free: 266,400 MB (260.1 GB > 15 GB threshold verified)."
        ),
    },
    {
        "role": "assistant",
        "content": "Free disk space confirmed (260.1 GB > 15 GB). Initiating batch 1 cursor extraction (records 0-500).",
        "tool_calls": [
            {
                "id": "call_batch1_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "python3 extract_cursor.py --table accounts_v2 --offset 0 --limit 500 --out /var/scratch/batch_staging/batch_00001.parquet"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_batch1_longer",
        "content": "Batch 1 extracted: 500 records written to /var/scratch/batch_staging/batch_00001.parquet. Checkpoint state updated in /var/data/migration_checkpoint.db.",
    },
    # Tail messages: subsequent ongoing operational turns
    {
        "role": "user",
        "content": "Please verify batch 1 row checksum against RocksDB before proceeding with subsequent batches.",
    },
    {
        "role": "assistant",
        "content": "Verifying batch 1 records against RocksDB checksum ledger.",
        "tool_calls": [
            {
                "id": "call_verify_b1_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "python3 verify_batch.py --batch /var/scratch/batch_staging/batch_00001.parquet"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_verify_b1_longer",
        "content": "Verification success: 500 of 500 records match sha256 checksum in /var/data/migration_checkpoint.db.",
    },
    {
        "role": "user",
        "content": "Proceed with extraction batches 2 through 10 in parallel worker mode.",
    },
    {
        "role": "assistant",
        "content": "Launching parallel extraction workers for batches 2 through 10.",
        "tool_calls": [
            {
                "id": "call_parallel_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "python3 launch_parallel_workers.py --start-batch 2 --end-batch 10 --workers 4"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_parallel_longer",
        "content": "Workers launched: PIDs [41021, 41022, 41023, 41024] active. Batches 2-10 processing.",
    },
    {
        "role": "user",
        "content": "What is the current memory and CPU overhead of the parallel workers?",
    },
    {
        "role": "assistant",
        "content": "Inspecting process resource utilization.",
        "tool_calls": [
            {
                "id": "call_proc_stat_longer",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "ps -o pid,rss,%cpu -p 41021,41022,41023,41024"}',
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_proc_stat_longer",
        "content": "  PID   RSS %CPU\n41021 98304  5.2\n41022 96120  4.8\n41023 97440  5.1\n41024 95800  4.9\nTotal RSS: ~387 MB. Resource utilization nominal.",
    },
    {
        "role": "user",
        "content": "Keep monitoring worker execution until batch 10 is completed.",
    },
    {
        "role": "assistant",
        "content": "Monitoring active. Batches 2 through 10 are at approximately 65% progress.",
    },
    {
        "role": "user",
        "content": "Awaiting batch completion notification.",
    },
]
