# langgraph-checkpoint-azure-sql

> A production-grade **LangGraph checkpoint saver for Azure SQL Database** (and any SQL Server engine) — with a real, reproducible benchmark suite.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Conformance](https://img.shields.io/badge/conformance-15%2F15_passing-brightgreen.svg)](tests/test_conformance.py)
[![Benchmarked](https://img.shields.io/badge/benchmarks-7_suites-orange.svg)](docs/CONFLUENCE.md)

LangGraph ships checkpoint savers for Postgres and SQLite — **not** SQL Server or Azure SQL. This library fills that gap for Azure-first and Windows enterprises, with the same 4-table design as the official Postgres saver and a benchmark suite whose results feed the docs automatically.

---

## Highlights

- ✅ **15/15** `BaseCheckpointSaver` conformance tests pass against a live engine
- ✅ **0 errors** across every concurrency benchmark (UPDLOCK/HOLDLOCK, no `MERGE`)
- ⚡ **~16.8 ms p50** for a full 3-node graph run (3 checkpoints persisted)
- 📦 **~46 KB per invocation** stored (~9.3 KB/checkpoint, with blob dedup)
- ☁️ **Same code** on Azure SQL Database and on-prem SQL Server — only the connection string changes
- 🔐 First-class **Azure AD / Managed Identity** support

---

## Install

```bash
pip install -e .
# benchmarks + docs extras:
pip install langgraph python-dotenv python-docx pytest
```

Requires the [Microsoft ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).

## Quick start

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

## Benchmarks (real, reproducible)

Seven suites run against the real database; the docs are generated from their JSON output, so they can't drift from the measurements.

| Suite | Measures | Headline result |
|---|---|---|
| `latency` | per-request + throughput | p50 **16.8 ms**, **63.7 req/s** concurrent |
| `db_size` | storage footprint | **~46 KB / invocation** |
| `correctness` | concurrency safety | 150 runs, **0 errors** |
| `payload_scaling` | 1 KB → 1 MB | flat < 100 KB |
| `history_depth` | 1 → 200 turns | `get_tuple` ~1 ms flat; `list` grows O(N) |
| `connection_pool` | pool 1 → 50 | 400 → **1,414 rps** |
| `pruning` | retention ops | **1.58 ms** single-thread delete |

```bash
set AZURE_SQL_CONN_STR=DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=...;...
python -m benchmarks.run_all        # → benchmarks/results/*.json
python scripts/generate_docx.py     # → docs/Azure_SQL_LangGraph_Checkpoint_Benchmarks.docx
```

> ⚠️ **Honesty note.** The default results were captured against a **local SQL Server 2022** database used as an Azure SQL stand-in (identical T-SQL engine). The result *shape* transfers directly to Azure SQL; *absolute* latency on a real `*.database.windows.net` endpoint adds network RTT and is governed by your service tier. Point `AZURE_SQL_CONN_STR` at a real endpoint and re-run to capture cloud numbers — the docs regenerate themselves. Full detail in [`docs/CONFLUENCE.md`](docs/CONFLUENCE.md).

## Documentation

| Document | What it is |
|---|---|
| [`docs/CONFLUENCE.md`](docs/CONFLUENCE.md) | The Confluence page — full benchmark results, limitations, production guide |
| [`docs/ENGINEERING_REFERENCE.md`](docs/ENGINEERING_REFERENCE.md) | Deep engineering reference — schema, T-SQL decisions, concurrency, async |
| `docs/Azure_SQL_LangGraph_Checkpoint_Benchmarks.docx` | **Word report**, generated from the real result JSON |

## Tests

```bash
set AZURE_SQL_TEST_CONN_STR=DRIVER={ODBC Driver 18 for SQL Server};SERVER=.;DATABASE=langgraph_azure_test;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;
pytest tests/test_conformance.py -v     # 15 passed
```

## Project layout

```
src/langgraph_checkpoint_azure_sql/   # the library (base, pool, saver)
tests/test_conformance.py             # 15 conformance tests
benchmarks/                           # 7 suites + run_all.py + _harness.py
benchmarks/results/                   # generated JSON (source of truth for docs)
scripts/generate_docx.py              # builds the Word report from results
docs/                                  # Confluence page, engineering ref, .docx
```

## Relationship to mssql-saver

The companion [`langgraph-checkpoint-mssql`](https://github.com/sandeshbagmare/mssql-saver) targets on-premises SQL Server. Same T-SQL, same schema, same concurrency model — different package name and audience. Both work against both targets.

## License

MIT — see [LICENSE](LICENSE).
