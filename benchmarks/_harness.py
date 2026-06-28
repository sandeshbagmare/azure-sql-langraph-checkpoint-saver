"""Shared benchmark harness for the Azure SQL checkpoint saver.

Provides:
- ``azure_conn_str()``     — connection string for the target DB (env-overridable)
- ``make_saver()``         — a configured AzureSqlSaver
- ``raw_conn()``           — a raw autocommit pyodbc connection (for size/DDL probes)
- ``build_graph(saver)``   — a real 3-node LangGraph graph for end-to-end runs
- ``percentiles()``        — latency percentile helper
- ``save_result()``        — write a result JSON into benchmarks/results/

IMPORTANT — what "Azure SQL" means in these benchmarks
------------------------------------------------------
These suites target the database named by ``AZURE_SQL_CONN_STR`` (default:
a local SQL Server 2022 database called ``langgraph_azure``). Azure SQL
Database runs the *identical T-SQL engine* as on-premises SQL Server, so a
local instance is a faithful functional stand-in for measuring the library's
behaviour (schema, concurrency model, query plans, serialization cost).

Absolute latency numbers will differ on a real Azure SQL deployment because of
network round-trip time and the service tier's IO/DTU governor. The *shape* of
the results — how latency scales with payload size, history depth, pool size,
and concurrency — transfers directly. Every doc generated from these results
states this explicitly.
"""
from __future__ import annotations

import os
import statistics
from pathlib import Path

import pyodbc

try:
    from dotenv import load_dotenv
    # Load the workspace .env (one level up from this repo folder) if present,
    # then any local .env — local wins.
    _here = Path(__file__).resolve()
    load_dotenv(_here.parents[2] / ".env")
    load_dotenv(_here.parents[1] / ".env")
except Exception:  # pragma: no cover - dotenv optional
    pass


DEFAULT_AZURE_CONN_STR = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=.;DATABASE=langgraph_azure;"
    "Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;"
    "MARS_Connection=yes;"
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def azure_conn_str() -> str:
    """Connection string for the Azure SQL (stand-in) database."""
    return os.environ.get("AZURE_SQL_CONN_STR", DEFAULT_AZURE_CONN_STR)


def make_saver(pool_size: int = 20):
    """Return a configured (and schema-migrated) AzureSqlSaver."""
    from langgraph_checkpoint_azure_sql import AzureSqlSaver

    saver = AzureSqlSaver(azure_conn_str(), pool_size=pool_size)
    saver.setup()
    return saver


def raw_conn(autocommit: bool = True) -> pyodbc.Connection:
    """A raw pyodbc connection (MARS enabled) for size/DDL probes."""
    cs = azure_conn_str()
    if "MARS_Connection" not in cs and "mars_connection" not in cs.lower():
        cs = cs.rstrip(";") + ";MARS_Connection=yes;"
    return pyodbc.connect(cs, autocommit=autocommit)


def is_real_azure() -> bool:
    return ".database.windows.net" in azure_conn_str().lower()


# ---------------------------------------------------------------------------
# A real, self-contained LangGraph workload (3 nodes → 3 checkpoints/run)
# ---------------------------------------------------------------------------

SAMPLE_TEXT = (
    "LangGraph is a library for building stateful, multi-actor applications with LLMs. "
    "It extends LangChain by providing a graph-based orchestration framework. "
    "Each node in the graph represents a computation step. "
    "The checkpointer persists state between steps, enabling resumable workflows."
)


def build_graph(saver):
    """Compile a small but real 3-node StateGraph backed by ``saver``.

    normalise -> count -> summarise. Each node mutates state, producing a
    checkpoint per step — the same write pattern a production agent generates.
    """
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        text: str
        normalised: str
        word_count: int
        char_count: int
        sentence_count: int
        summary: str

    def normalise(state: State) -> dict:
        return {"normalised": " ".join(state["text"].split()).strip()}

    def count(state: State) -> dict:
        norm = state["normalised"]
        return {
            "word_count": len(norm.split()),
            "char_count": len(norm),
            "sentence_count": norm.count(".") + norm.count("!") + norm.count("?"),
        }

    def summarise(state: State) -> dict:
        return {
            "summary": (
                f"{state['word_count']} words, {state['char_count']} chars, "
                f"{state['sentence_count']} sentences."
            )
        }

    g = StateGraph(State)
    g.add_node("normalise", normalise)
    g.add_node("count", count)
    g.add_node("summarise", summarise)
    g.add_edge(START, "normalise")
    g.add_edge("normalise", "count")
    g.add_edge("count", "summarise")
    g.add_edge("summarise", END)
    return g.compile(checkpointer=saver)


def initial_state() -> dict:
    return {
        "text": SAMPLE_TEXT,
        "normalised": "",
        "word_count": 0,
        "char_count": 0,
        "sentence_count": 0,
        "summary": "",
    }


# ---------------------------------------------------------------------------
# Stats + IO helpers
# ---------------------------------------------------------------------------

def percentiles(latencies: list[float]) -> dict:
    if not latencies:
        return {}
    s = sorted(latencies)
    n = len(s)

    def at(p: float) -> float:
        idx = min(n - 1, int(n * p))
        return round(s[idx], 2)

    total_s = sum(latencies) / 1000.0
    return {
        "n": n,
        "mean": round(statistics.mean(s), 2),
        "p50": at(0.50),
        "p90": at(0.90),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": round(s[-1], 2),
        "throughput_rps": round(n / total_s, 1) if total_s > 0 else 0.0,
    }


def save_result(name: str, payload: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{name}.json"
    import json
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    return out
