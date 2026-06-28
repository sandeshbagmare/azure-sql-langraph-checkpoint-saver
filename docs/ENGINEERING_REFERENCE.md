# Azure SQL LangGraph Checkpoint Saver — Engineering Reference

**Deep engineering reference for `langgraph-checkpoint-azure-sql`.** For the benchmark
results and production guidance, see [`CONFLUENCE.md`](CONFLUENCE.md) and the generated
Word report `Azure_SQL_LangGraph_Checkpoint_Benchmarks.docx`.

| | |
|---|---|
| Library | `langgraph-checkpoint-azure-sql` v0.1.0 |
| Companion | `langgraph-checkpoint-mssql` (on-prem SQL Server) |
| Engine | Azure SQL Database / SQL Server 2016+ |
| Driver | ODBC Driver 18 for SQL Server |
| Python | 3.10+ (tested on 3.13) |

---

## Table of Contents

1. [Design goals](#1-design-goals)
2. [Four-table schema](#2-four-table-schema)
3. [Azure SQL / T-SQL engineering decisions](#3-azure-sql--t-sql-engineering-decisions)
4. [Connection string recipes](#4-connection-string-recipes)
5. [Concurrency model](#5-concurrency-model)
6. [Async design](#6-async-design)
7. [Serialization model](#7-serialization-model)
8. [Benchmark methodology](#8-benchmark-methodology)
9. [Results summary](#9-results-summary)
10. [Relationship to mssql-saver](#10-relationship-to-mssql-saver)
11. [Appendix: full SQL schema](#11-appendix-full-sql-schema)

---

## 1. Design goals

1. **Drop-in `BaseCheckpointSaver`** — works anywhere LangGraph accepts a checkpointer.
2. **One codebase, two targets** — identical T-SQL for Azure SQL Database and on-prem SQL Server.
3. **Injection-proof** — every value is a `?` parameter, including `OFFSET/FETCH` limits.
4. **Concurrency-correct without `MERGE`** — UPDLOCK/HOLDLOCK upserts, proven by a zero-error concurrency benchmark.
5. **Faithful to the official Postgres saver** — same 4-table split, same dedup semantics, so behaviour is predictable to anyone who knows the Postgres saver.

---

## 2. Four-table schema

```
checkpoint_migrations   v (PK)                          ← idempotent DDL versioning
checkpoints             (thread_id, ns, checkpoint_id)  ← envelope + metadata (no channel values)
checkpoint_blobs        (thread_id, ns, channel, ver)   ← channel values, deduplicated by version
checkpoint_writes       (thread_id, ns, cp_id, task, idx) ← pending/intermediate writes
```

- **`checkpoints`** stores the serialized checkpoint *envelope* with `channel_values` popped out — small rows, fast latest-fetch.
- **`checkpoint_blobs`** stores each channel value once per `(channel, version)`. Unchanged channels across steps are **not** re-stored — this is the dedup that keeps storage at ~9.3 KB/checkpoint instead of re-serializing full state every step.
- **`checkpoint_writes`** holds mid-step task output; LangGraph cleans it up as steps commit.

Composite primary keys (no surrogate identity) match the Postgres design and avoid hot-page insert contention from a monotonic identity column.

---

## 3. Azure SQL / T-SQL engineering decisions

### 3.1 `checkpoint` is a reserved keyword
`CHECKPOINT` is a T-SQL statement. The table and the column are bracket-quoted everywhere: `[checkpoints]`, `[checkpoint]`. Any raw query or ORM that doesn't quote identifiers will fail on this column.

### 3.2 No `MERGE`
T-SQL `MERGE` has documented concurrency bugs (phantom inserts under race). Upserts are implemented as:

```sql
UPDATE [checkpoints] WITH (UPDLOCK, HOLDLOCK)
SET [checkpoint]=?, metadata=?
WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?;
-- if @@ROWCOUNT = 0 → INSERT
```

Blobs use a **DO-NOTHING** upsert (`INSERT … WHERE NOT EXISTS`) because a `(channel, version)` blob is immutable once written.

### 3.3 MARS is required
`_row_to_tuple()` opens sub-cursors (blob + write lookups) while the main cursor is open. That needs **Multiple Active Result Sets**. The pool appends `MARS_Connection=yes` if absent. Cost: ~1–3%/op; non-negotiable.

### 3.4 `OFFSET/FETCH`, not `LIMIT`
SQL Server has no `LIMIT ?`. Pagination uses `ORDER BY checkpoint_id DESC OFFSET 0 ROWS FETCH NEXT ? ROWS ONLY`, with the limit as a **parameter** — never string-concatenated.

### 3.5 `JSON_VALUE` metadata filtering
`list(filter={"source": "loop"})` becomes `WHERE JSON_VALUE(metadata, ?) = ?`. Requires SQL Server 2016+ (compat level 130). Azure SQL always qualifies.

### 3.6 Version strings sort lexicographically
`get_next_version()` emits `f"{v+1:032}.{random:016}"` — the 32-digit zero-pad makes string sort equal numeric sort, and the random suffix avoids cross-writer collisions without a DB sequence.

### 3.7 `is_azure` detection
The pool exposes `is_azure` (`.database.windows.net` in the connection string) so callers/telemetry can branch on deployment target without extra config.

---

## 4. Connection string recipes

```python
# SQL auth (dev)
"DRIVER={ODBC Driver 18 for SQL Server};SERVER=srv.database.windows.net;"
"DATABASE=langgraph;UID=user;PWD=pass;Encrypt=yes;TrustServerCertificate=no;"

# Azure AD interactive (developer SSO)
"...;SERVER=srv.database.windows.net;DATABASE=langgraph;"
"Authentication=ActiveDirectoryInteractive;Encrypt=yes;"

# Managed Identity (production — App Service / AKS / VM)
"...;SERVER=srv.database.windows.net;DATABASE=langgraph;"
"Authentication=ActiveDirectoryMsi;Encrypt=yes;"

# Service Principal (CI/CD)
"...;Authentication=ActiveDirectoryServicePrincipal;UID=<app-id>;PWD=<secret>;"

# On-prem SQL Server (Windows auth)
"DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=langgraph;"
"Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;"
```

---

## 5. Concurrency model

- Writes to the **same** `thread_id` serialize via UPDLOCK/HOLDLOCK — correct, because LangGraph does not issue concurrent writes to one thread.
- Writes to **different** threads run fully concurrently, bounded only by the connection pool.
- The **correctness benchmark** (30 threads × 5 invocations = 150 runs) and the conformance `test_concurrent_writes` (20 threads) both complete with **0 errors / 0 PK violations**.
- The pool commits on clean exit and **rolls back + discards** a connection on any exception, so a mid-write failure never returns a poisoned connection to the pool.

---

## 6. Async design

`AzureSqlSaver` exposes `aget_tuple` / `alist` / `aput` / `aput_writes` / `adelete_thread`, each delegating to the sync method via `asyncio.to_thread`. This deliberately avoids `aioodbc` (low release cadence) in favour of well-maintained `pyodbc` + a thin shim.

**Implication:** async calls get *scheduling* concurrency, not true non-blocking IO. Under heavy async fan-out, the default thread pool — not the database — is the limiter. For coroutine-heavy services, prefer the **sync** saver with a thread pool you size yourself.

---

## 7. Serialization model

- Uses LangGraph's default `JsonPlusSerializer` (msgpack + typed fallback).
- `_serialize_checkpoint()` pops `channel_values`, serializes the envelope to `VARBINARY(MAX)`, and emits `(channel, version, type, blob)` tuples for the changed channels only.
- `VARBINARY(MAX)` stores up to 2 GB; values > 8 KB spill to LOB pages transparently (one extra page read). The [payload benchmark](CONFLUENCE.md#b4) shows this is negligible below 100 KB.

---

## 8. Benchmark methodology

Seven suites, all in `benchmarks/`, all reading/writing the **real** target DB:

| Suite | Isolates | Method |
|---|---|---|
| `latency` | per-request + throughput | real 3-node graph, sequential + concurrent |
| `db_size` | storage cost | seed 300 runs, diff `sp_spaceused` |
| `correctness` | concurrency safety | 30 threads, assert get/list integrity |
| `payload_scaling` | serialization + IO | saver `put`/`get` from 1 KB → 1 MB |
| `history_depth` | index efficiency | get vs list at 1…200 turns |
| `connection_pool` | pool sizing | fixed load, pools 1…50 |
| `pruning` | retention ops | `delete_thread` single + bulk |

`run_all.py` orchestrates all seven and writes `results/FULL_REPORT.json`. `scripts/generate_docx.py` reads that JSON to emit the Word report — **the documents cannot drift from the measurements**.

> **Stand-in note:** the default target is a local SQL Server 2022 DB (`langgraph_azure`). Same engine as Azure SQL; absolute latency excludes cloud RTT/tier governance. See the honesty note in `CONFLUENCE.md`.

---

## 9. Results summary

Captured at default scale (full numbers in [`CONFLUENCE.md`](CONFLUENCE.md)):

| Dimension | Headline |
|---|---|
| Sequential latency | p50 **16.8 ms** / 3-node run (~5.6 ms/checkpoint) |
| Concurrent throughput | **63.7 req/s** (~190 checkpoints/s), 0 errors |
| Storage | **~46 KB/invocation**, ~9.3 KB/checkpoint |
| Correctness | 150 concurrent runs, **0 errors** |
| Payload | flat < 100 KB; ~7.6× put p50 at 1 MB |
| History depth | `get_tuple` ~1 ms flat; `list` → ~125 ms @ 200 turns |
| Pool sizing | 400 rps (pool 1) → **1,414 rps** (pool 50) |
| Pruning | **1.58 ms** single-thread delete; ~130 ms / 1k rows |
| Conformance | **15/15 pass** |

---

## 10. Relationship to mssql-saver

| Aspect | mssql-saver | azure-sql-saver (this) |
|---|---|---|
| Package | `langgraph-checkpoint-mssql` | `langgraph-checkpoint-azure-sql` |
| Import | `from langgraph_checkpoint_mssql import MssqlSaver` | `from langgraph_checkpoint_azure_sql import AzureSqlSaver` |
| T-SQL | identical | identical |
| Schema | same 4 tables | same 4 tables |
| Azure AD / MSI | works (undocumented) | documented + `is_azure` helper |
| Audience | on-prem SQL Server | Azure-first enterprises |
| Works on the other target? | yes | yes |

**Functionally interchangeable.** Choose by deployment target and naming convention.

---

## 11. Appendix: full SQL schema

```sql
-- 0: migration versioning
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='checkpoint_migrations')
CREATE TABLE checkpoint_migrations (v INT NOT NULL, CONSTRAINT PK_cm PRIMARY KEY (v));

-- 1: checkpoints  ('checkpoint' is reserved → bracket-quoted)
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='checkpoints')
CREATE TABLE [checkpoints] (
    thread_id            NVARCHAR(150)  NOT NULL,
    checkpoint_ns        NVARCHAR(255)  NOT NULL DEFAULT '',
    checkpoint_id        NVARCHAR(150)  NOT NULL,
    parent_checkpoint_id NVARCHAR(150)  NULL,
    type                 NVARCHAR(150)  NULL,
    [checkpoint]         VARBINARY(MAX) NOT NULL,
    metadata             NVARCHAR(MAX)  NOT NULL DEFAULT '{}',
    CONSTRAINT PK_checkpoints PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

-- 2: channel blobs (deduplicated by version)
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='checkpoint_blobs')
CREATE TABLE [checkpoint_blobs] (
    thread_id     NVARCHAR(150)  NOT NULL,
    checkpoint_ns NVARCHAR(255)  NOT NULL,
    channel       NVARCHAR(255)  NOT NULL,
    version       NVARCHAR(150)  NOT NULL,
    type          NVARCHAR(150)  NOT NULL,
    blob          VARBINARY(MAX) NULL,
    CONSTRAINT PK_checkpoint_blobs PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

-- 3: pending writes
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='checkpoint_writes')
CREATE TABLE [checkpoint_writes] (
    thread_id     NVARCHAR(150)  NOT NULL,
    checkpoint_ns NVARCHAR(255)  NOT NULL,
    checkpoint_id NVARCHAR(150)  NOT NULL,
    task_id       NVARCHAR(150)  NOT NULL,
    idx           INT            NOT NULL,
    channel       NVARCHAR(255)  NOT NULL,
    type          NVARCHAR(150)  NULL,
    blob          VARBINARY(MAX) NOT NULL,
    task_path     NVARCHAR(MAX)  NOT NULL DEFAULT '',
    CONSTRAINT PK_checkpoint_writes
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- 4-6: thread_id indexes
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_checkpoints_tid')
    CREATE INDEX IX_checkpoints_tid ON [checkpoints](thread_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_cb_tid')
    CREATE INDEX IX_cb_tid ON [checkpoint_blobs](thread_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_cw_tid')
    CREATE INDEX IX_cw_tid ON [checkpoint_writes](thread_id);
```

---

*Generated alongside the benchmark run. To refresh: `python -m benchmarks.run_all && python scripts/generate_docx.py`.*
