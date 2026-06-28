"""Benchmark 2/7: database storage footprint (Azure SQL).

Seeds N end-to-end graph runs, then measures per-table row counts and
reserved size via sp_spaceused, and computes per-invocation storage cost.

Usage:
    python -m benchmarks.db_size
    python -m benchmarks.db_size --runs 500
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks._harness import build_graph, initial_state, make_saver, raw_conn, save_result

TABLES = ["checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"]


def table_sizes(cur) -> dict:
    out = {}
    for t in TABLES:
        cur.execute(f"EXEC sp_spaceused '{t}'")
        row = cur.fetchone()
        if row:
            reserved_kb = int(row[2].strip().replace(" KB", ""))
            out[t] = {"rows": int(row[1]), "size_bytes": reserved_kb * 1024}
        else:
            out[t] = {"rows": 0, "size_bytes": 0}
    cur.execute("SELECT SUM(size * 8 * 1024) FROM sys.database_files WHERE type_desc='ROWS'")
    row = cur.fetchone()
    out["_total_db"] = {"rows": None, "size_bytes": int(row[0]) if row and row[0] else 0}
    return out


def main():
    ap = argparse.ArgumentParser(description="Azure SQL storage footprint")
    ap.add_argument("--runs", type=int, default=300, help="Graph invocations to seed")
    args = ap.parse_args()

    saver = make_saver(pool_size=20)
    graph = build_graph(saver)

    conn = raw_conn()
    cur = conn.cursor()
    before = table_sizes(cur)

    print(f"[db_size] seeding {args.runs} graph runs ...")
    tids = []
    for i in range(args.runs):
        tid = f"size-{uuid.uuid4()}"
        tids.append(tid)
        graph.invoke(initial_state(), {"configurable": {"thread_id": tid}})
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{args.runs}")

    after = table_sizes(cur)

    # Delta attributable to the seeded runs
    delta = {}
    for t in TABLES:
        delta[t] = {
            "rows": after[t]["rows"] - before[t]["rows"],
            "size_bytes": after[t]["size_bytes"] - before[t]["size_bytes"],
        }

    seeded_ckpts = delta["checkpoints"]["rows"] or 1
    per_invocation_bytes = sum(delta[t]["size_bytes"] for t in TABLES) / args.runs

    def human(b):
        b = float(b)
        if abs(b) < 1024:
            return f"{b:.0f} B"
        if abs(b) < 1_048_576:
            return f"{b / 1024:.1f} KB"
        return f"{b / 1_048_576:.2f} MB"

    print(f"\n{'Table':<26}{'rows (delta)':>14}{'size (delta)':>16}")
    print("-" * 56)
    for t in TABLES:
        print(f"{t:<26}{delta[t]['rows']:>14}{human(delta[t]['size_bytes']):>16}")
    print("-" * 56)
    print(f"{'per invocation':<26}{'':>14}{human(per_invocation_bytes):>16}")
    print(f"{'total DB now':<26}{'':>14}{human(after['_total_db']['size_bytes']):>16}\n")

    # Cleanup seeded threads
    print("[db_size] cleaning up seeded threads ...")
    for tid in tids:
        try:
            saver.delete_thread(tid)
        except Exception:
            pass

    payload = {
        "args": vars(args),
        "before": before,
        "after": after,
        "delta": delta,
        "per_invocation_bytes": round(per_invocation_bytes, 1),
        "per_checkpoint_bytes": round(
            sum(delta[t]["size_bytes"] for t in TABLES) / seeded_ckpts, 1
        ),
    }
    out = save_result("db_size", payload)
    print(f"saved -> {out}")
    conn.close()
    saver.pool.close()
    return payload


if __name__ == "__main__":
    main()
