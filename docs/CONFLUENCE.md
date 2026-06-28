# Azure SQL Database — LangGraph Checkpoint Saver

> **Confluence Page** · Engineering · AI Platform
> Owner: Sandesh Bagmare / Pawan Nala · Last updated: June 2026 · Status: ✅ Benchmarked

A production-grade LangGraph checkpoint saver for **Azure SQL Database** (and any SQL Server engine). This page collects the design, the **real benchmark results**, limitations, and production guidance for running LangGraph agents on Azure SQL.

> 🔬 **Every number on this page is generated.** The figures below come from `python -m benchmarks.run_all` reading its result JSON — not hand-typed. Re-run the suite and regenerate to refresh them.

---

## 📑 Contents

1. [TL;DR](#tldr)
2. [What this is](#what-this-is)
3. [What the benchmarks measure (honesty note)](#honesty)
4. [Test environment](#environment)
5. [Benchmark 1 — Latency & throughput](#b1)
6. [Benchmark 2 — Storage footprint](#b2)
7. [Benchmark 3 — Correctness under concurrency](#b3)
8. [Benchmark 4 — Payload size scaling](#b4)
9. [Benchmark 5 — History depth scaling](#b5)
10. [Benchmark 6 — Connection pool sizing](#b6)
11. [Benchmark 7 — Pruning / retention](#b7)
12. [Conformance](#conformance)
13. [Limitations](#limitations)
14. [Production recommendations](#production)
15. [How to reproduce](#reproduce)

---

<a name="tldr"></a>
## 1. TL;DR

- ✅ **15/15 conformance tests pass** against a live SQL Server / Azure SQL engine.
- ✅ **0 errors** across every concurrency benchmark (UPDLOCK/HOLDLOCK upserts, no MERGE).
- ⚡ **Sequential p50 ≈ 16.8 ms** for a full 3-node graph run (3 checkpoints persisted).
- ⚡ **~46 KB stored per graph invocation** (3 checkpoints); ~9.3 KB per checkpoint.
- 📈 **`get_tuple()` is flat** as history grows (indexed); **`list()` grows linearly** — keep it out of hot paths.
- 🔌 **Pool size ≈ peak workers** — throughput climbs from ~400 rps (pool 1) to ~1,400 rps (pool 50).
- ☁️ **Same code on Azure SQL and on-prem SQL Server** — only the connection string changes.

---

<a name="what-this-is"></a>
## 2. What this is

[LangGraph](https://langchain-ai.github.io/langgraph/) persists agent state through a **checkpoint saver**. LangGraph ships savers for Postgres and SQLite — **not** SQL Server or Azure SQL. This library fills that gap for Azure-first and Windows enterprises.

```python
from langgraph_checkpoint_azure_sql import AzureSqlSaver

CONN_STR = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=yourserver.database.windows.net;"
    "DATABASE=langgraph;"
    "Authentication=ActiveDirectoryMsi;"   # passwordless on Azure
    "Encrypt=yes;"
)

with AzureSqlSaver(CONN_STR, pool_size=20) as saver:
    saver.setup()                          # idempotent schema migration
    graph = builder.compile(checkpointer=saver)
    graph.invoke({"text": "hello"}, {"configurable": {"thread_id": "t1"}})
```

**Schema** — four tables mirroring the official Postgres saver: `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`.

---

<a name="honesty"></a>
## 3. What the benchmarks measure (honesty note)

> ⚠️ **Read this before quoting the numbers.**

These results were captured against a **local Microsoft SQL Server 2022 instance** (database `langgraph_azure`), used as an **Azure SQL stand-in**. Azure SQL Database runs the *identical T-SQL engine*, so this faithfully exercises the schema, the UPDLOCK/HOLDLOCK concurrency model, query plans, `JSON_VALUE` filtering, MARS, and serialization cost.

| Transfers directly to real Azure SQL | Will differ on real Azure SQL |
|---|---|
| Result **shape** — scaling vs payload, depth, pool, concurrency | **Absolute latency** (adds network RTT) |
| Storage cost per checkpoint | Latency under DTU/vCore governance |
| Zero-error concurrency guarantee | Throughput ceiling per service tier |
| Schema, indexes, query plans | Cold-start / auto-pause on Serverless |

Treat the millisecond figures here as a **local-loopback floor** and add your region's network RTT (typically 1–10 ms intra-region) plus tier overhead. To capture true cloud numbers, point `AZURE_SQL_CONN_STR` at a real endpoint and re-run — the docs regenerate from the new JSON.

---

<a name="environment"></a>
## 4. Test environment

| Parameter | Value |
|---|---|
| Engine | Microsoft SQL Server 2022 (16.0) — Azure SQL engine equivalent |
| Driver | ODBC Driver 18 for SQL Server (`Encrypt=yes; MARS_Connection=yes`) |
| Client | Windows 11, local loopback |
| Workload | Real 3-node LangGraph `StateGraph` (normalise → count → summarise) |
| Checkpoints / run | 3 (one per node) |
| Serializer | `JsonPlusSerializer` (msgpack) — LangGraph default |
| Default scale | latency n=300, db_size 300 runs, correctness 30×5, pool 20w/200ops, pruning 100×20 |

---

<a name="b1"></a>
## 5. Benchmark 1 — Latency & throughput

Each invocation runs the full graph and commits three checkpoints.

| Scenario | n | mean | p50 | p95 | p99 | max | rps |
|---|---|---|---|---|---|---|---|
| sequential | 300 | 18.2 | **16.8** | 27.4 | 50.9 | 78.5 | 54.9 |
| concurrent (15 workers) | 300 | 233.4 | 212.6 | 387.7 | 451.7 | 485.1 | **63.7** |

*All latencies in ms; throughput is wall-clock req/s. 0 errors.*

**Reading it:** single-thread p50 is **16.8 ms** for three persisted checkpoints (~5.6 ms/checkpoint). The concurrent run sustains **63.7 req/s** → roughly **190 checkpoint writes/s** on a local single instance. On Azure SQL, expect lower absolute throughput per instance (network + tier) but the same horizontal-scaling behaviour: add app instances, raise the tier.

---

<a name="b2"></a>
## 6. Benchmark 2 — Storage footprint

Measured by seeding 300 full graph invocations and diffing `sp_spaceused`.

| Table | rows added | size added |
|---|---|---|
| checkpoints | 1,500 | 3.50 MB |
| checkpoint_blobs | 5,700 | 5.38 MB |
| checkpoint_writes | 4,200 | 4.69 MB |
| checkpoint_migrations | 0 | 0 B |

| Unit | Storage |
|---|---|
| **Per full invocation (3 checkpoints)** | **~46.3 KB** |
| Per checkpoint | ~9.3 KB |

**Projected growth (no pruning):**

| Invocation rate | Monthly | Annual |
|---|---|---|
| 1,000 / day | ~1.4 GB | ~16.6 GB |
| 10,000 / day | ~14 GB | ~166 GB |
| 100,000 / day | ~136 GB | ~1.6 TB |

> 💡 `checkpoint_blobs` grows fastest (3.8 rows per checkpoint). Storage is real money on Azure SQL — implement [retention](#b7).

---

<a name="b3"></a>
## 7. Benchmark 3 — Correctness under concurrency

| Threads | Invocations/thread | Total | Result |
|---|---|---|---|
| 30 | 5 | 150 | ✅ **PASS — 0 errors** |

Each thread drives its own `thread_id`, then asserts `get_tuple()` returns the latest non-empty checkpoint and `list()` returns a non-empty descending history. The **UPDLOCK/HOLDLOCK** upsert (UPDATE-then-INSERT, never `MERGE`) eliminates the phantom-insert and lost-update races a naïve upsert would expose.

---

<a name="b4"></a>
## 8. Benchmark 4 — Payload size scaling

`put()` / `get_tuple()` latency as one channel value grows 1 KB → 1 MB.

| Payload | put p50 | put p95 | get p50 | get p95 | put rps |
|---|---|---|---|---|---|
| 1 KB | 1.52 | 83.01* | 0.97 | 53.38* | 174.2 |
| 10 KB | 1.58 | 11.60 | 1.07 | 5.88 | 416.8 |
| 50 KB | 1.80 | 3.61 | 1.24 | 2.70 | 502.3 |
| 100 KB | 2.14 | 13.07 | 1.63 | 7.90 | 344.1 |
| 500 KB | 4.73 | 11.36 | 4.11 | 7.55 | 192.9 |
| 1 MB | 11.57 | 23.35 | 8.42 | 17.26 | 79.5 |

*All latencies in ms. \*The 1 KB p95 reflects cold-pool warmup on the first batch, not payload cost.*

**Reading it:** below 100 KB, size is **negligible** — latency is round-trip-bound. From 1 KB → 1 MB, put p50 rises ~7.6×. **Store summaries/references in state, not raw documents.** Real LangGraph state is usually 1–50 KB — squarely in the optimal band.

---

<a name="b5"></a>
## 9. Benchmark 5 — History depth scaling

Does latency grow as a thread accumulates checkpoints?

| Depth (turns) | get p50 | get p95 | list p50 | list p95 |
|---|---|---|---|---|
| 1 | 0.88 | 4.07 | 0.93 | 2.24 |
| 5 | 0.91 | 4.92 | 3.16 | 7.25 |
| 10 | 0.90 | 1.19 | 6.07 | 16.22 |
| 25 | 0.95 | 1.37 | 15.33 | 16.44 |
| 50 | 1.79 | 75.74 | 37.22 | 50.95 |
| 100 | 1.88 | 174.39 | 126.32 | 354.96 |
| 200 | 1.13 | 25.59 | 125.45 | 218.22 |

*Latencies in ms.*

**Reading it:** `get_tuple()` (latest, indexed) stays **~1 ms flat** from 1 to 200 turns — the `ORDER BY checkpoint_id DESC` + PK index gives effectively O(log N). `list()` (full scan) climbs to **~125 ms p50** at 200 turns because it transfers every row. **Don't call `list()` in a hot loop**; paginate with `limit`/`before`, or cap history before pruning.

---

<a name="b6"></a>
## 10. Benchmark 6 — Connection pool sizing

Fixed load: 20 workers, 200 operations, varying pool size.

| Pool size | p50 | p95 | rps | errors |
|---|---|---|---|---|
| 1 | 19.5 | 83.7 | 400.8 | 0 |
| 5 | 8.1 | 61.5 | 914.9 | 0 |
| 10 | 8.0 | 95.6 | 785.2 | 0 |
| 20 | 6.3 | 113.2 | 845.5 | 0 |
| 50 | 6.6 | 18.8 | **1,413.8** | 0 |

*Latencies in ms.*

**Reading it:** a pool of 1 throttles 20 workers to ~400 rps; matching the pool to the worker count removes the queue. **Rule of thumb: `pool_size ≈ peak concurrent workers` (at most 2×).** On Azure SQL, keep `pool_size × instances` under the tier's [max connection budget](#production).

---

<a name="b7"></a>
## 11. Benchmark 7 — Pruning / retention

Seeded 100 threads × 20 turns = 2,000 checkpoints.

| Operation | Time | Scope |
|---|---|---|
| Single-thread `delete_thread()` | **1.58 ms** | 20 turns |
| Bulk delete (loop) | 257.42 ms | 1,980 rows / 99 threads |
| Per-thread delete | 2.6 ms | — |
| Per 1,000 rows | ~130 ms | — |

**Age-based retention** (schedule as a SQL Agent / cron job):

```sql
DELETE FROM [checkpoint_writes]
WHERE thread_id IN (
  SELECT DISTINCT thread_id FROM [checkpoints]
  WHERE TRY_CAST(JSON_VALUE(metadata,'$.created_at') AS DATETIME2)
        < DATEADD(DAY, -30, GETUTCDATE())
);
DELETE FROM [checkpoint_blobs] WHERE thread_id NOT IN (SELECT DISTINCT thread_id FROM [checkpoints]);
DELETE FROM [checkpoints]
WHERE TRY_CAST(JSON_VALUE(metadata,'$.created_at') AS DATETIME2)
      < DATEADD(DAY, -30, GETUTCDATE());
```

> Delete in batches of ~10,000 rows to avoid long transactions and lock escalation.

---

<a name="conformance"></a>
## 12. Conformance

```
pytest tests/test_conformance.py -v   →   15 passed
```

Covers: put/get round-trips (latest & by-id), parent-config chaining, list ordering/limit/before/metadata-filter, `put_writes` dedup (DO-UPDATE for idx≥0, DO-NOTHING for idx<0), cascading `delete_thread`, version monotonicity, 20-thread concurrent writes, and the async wrappers (`aput`/`aget_tuple`/`alist`).

---

<a name="limitations"></a>
## 13. Limitations

| Limitation | Detail |
|---|---|
| **No native async IO** | `pyodbc` is sync; async methods wrap `asyncio.to_thread`. The thread pool, not the DB, bounds async concurrency. |
| **MARS required** | Sub-cursors for blob/write lookups need `MARS_Connection=yes` (auto-appended); ~1–3% overhead, can't be disabled. |
| **`checkpoint` is reserved T-SQL** | Bracket-quoted everywhere (`[checkpoints]`, `[checkpoint]`). Tools/ORMs that don't quote identifiers break — use the library API. |
| **`JSON_VALUE` needs SQL Server 2016+** | Metadata filtering requires compat level 130+. Azure SQL always qualifies. |
| **No built-in retention** | LangGraph never prunes; tables grow unbounded without a scheduled job. |
| **Same-thread writes serialize** | UPDLOCK/HOLDLOCK serializes concurrent writes to one `thread_id` by design. |
| **Local-stand-in latency floor** | Absolute ms exclude cloud RTT and tier governance. |

---

<a name="production"></a>
## 14. Production recommendations (Azure SQL)

### Service tier

| Workload | Tier | Notes |
|---|---|---|
| Dev / PoC | Basic (5 DTU) or Serverless GP | Auto-pause saves idle cost |
| Light (≤10 agents) | Standard S1 (20 DTU) | Watch DTU% on bursts |
| Medium (≤50 agents) | Standard S3 (100 DTU) / GP Gen5-2 | vCore = predictable IO |
| Heavy (100+ agents) | Premium P2 / GP Gen5-4 | Higher log throughput |
| High-scale | Hyperscale | Independent compute/storage |

### Connection budget

| Tier | Max connections | Suggested `pool_size` |
|---|---|---|
| Basic | 30 | ≤ 10 |
| S1 | 60 | ≤ 20 |
| S3 | 400 | ≤ 50 |
| P2 | 1,600 | ≤ 100 |

### Security checklist

- ✅ **Managed Identity** (`Authentication=ActiveDirectoryMsi`) — no secrets in connection strings
- ✅ Azure AD-only auth; disable SQL auth in production
- ✅ Private Endpoint; disable public network access
- ✅ `Encrypt=yes` + `TrustServerCertificate=no`
- ✅ Least privilege: `db_datareader` + `db_datawriter` + DDL on the 4 tables only
- ✅ TDE (default on Azure SQL) for blobs at rest
- ✅ Never store secrets/PII in LangGraph metadata

### Monitoring

- Azure Monitor alert on DTU/CPU > 80% (5 min)
- Alert when connections approach `pool_size × instances`
- Query Performance Insight on the upsert + latest-fetch queries
- Track `get_tuple` p99 (< 50 ms healthy), `put` p99 (< 100 ms healthy)

---

<a name="reproduce"></a>
## 15. How to reproduce

```bash
pip install -e .
pip install langgraph python-dotenv python-docx pytest

# point at your DB (local SQL Server or real Azure SQL)
set AZURE_SQL_CONN_STR=DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=...;...

python -m benchmarks.run_all        # writes benchmarks/results/*.json
python scripts/generate_docx.py     # regenerates the Word report from that JSON
pytest tests/test_conformance.py -v # 15 passed
```

Point `AZURE_SQL_CONN_STR` at a real `*.database.windows.net` endpoint to capture true cloud latency, then regenerate — this page's companion Word document updates itself from the new results.

---

*Companion library for on-premises SQL Server: [`langgraph-checkpoint-mssql`](https://github.com/sandeshbagmare/mssql-saver). Same engine, same code, different package name.*
