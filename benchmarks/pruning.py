"""Benchmark 7/7: pruning / DELETE performance (Azure SQL).

Seeds N threads x M turns, then measures per-thread delete_thread() latency
and bulk DELETE throughput — the operations a retention job depends on.

Usage:
    python -m benchmarks.pruning
    python -m benchmarks.pruning --threads 100 --turns 20
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.base import empty_checkpoint

from benchmarks._harness import make_saver, raw_conn, save_result


def _ckpt(idx: int) -> dict:
    c = empty_checkpoint()
    c["channel_values"] = {"counter": idx}
    c["channel_versions"] = {"counter": f"{idx + 1:032}.0"}
    return c


def _size_kb(cur, table: str) -> float:
    cur.execute(f"EXEC sp_spaceused '{table}'")
    row = cur.fetchone()
    return int(row[2].strip().replace(" KB", "")) if row else 0.0


def main():
    ap = argparse.ArgumentParser(description="Azure SQL pruning/DELETE perf")
    ap.add_argument("--threads", type=int, default=100)
    ap.add_argument("--turns", type=int, default=20)
    args = ap.parse_args()

    saver = make_saver(pool_size=20)
    conn = raw_conn()
    cur = conn.cursor()

    total = args.threads * args.turns
    print(f"[pruning] seeding {args.threads} threads x {args.turns} turns = {total} checkpoints ...")
    tids = []
    for _ in range(args.threads):
        tid = f"prune-{uuid.uuid4()}"
        tids.append(tid)
        config = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
        for i in range(args.turns):
            ckpt = _ckpt(i)
            saver.put(config, ckpt, {"step": i}, ckpt["channel_versions"])

    size_before = _size_kb(cur, "checkpoints")

    # 1. Single-thread delete (delete_thread cascades over all 3 tables)
    t0 = time.perf_counter()
    saver.delete_thread(tids[0])
    single_ms = (time.perf_counter() - t0) * 1000

    # 2. Bulk delete the rest, timed
    rest = tids[1:]
    t0 = time.perf_counter()
    for tid in rest:
        saver.delete_thread(tid)
    bulk_ms = (time.perf_counter() - t0) * 1000

    size_after = _size_kb(cur, "checkpoints")

    rows_bulk = len(rest) * args.turns
    result = {
        "args": vars(args),
        "seeded_checkpoints": total,
        "single_thread_delete_ms": round(single_ms, 2),
        "bulk_delete_total_ms": round(bulk_ms, 2),
        "bulk_delete_threads": len(rest),
        "bulk_delete_rows": rows_bulk,
        "per_thread_delete_ms": round(bulk_ms / max(len(rest), 1), 3),
        "per_1k_rows_ms": round(bulk_ms / max(rows_bulk, 1) * 1000, 2),
        "size_kb_before": size_before,
        "size_kb_after": size_after,
        "retention_sql_example": (
            "DELETE FROM [checkpoint_writes] WHERE thread_id IN ("
            "SELECT DISTINCT thread_id FROM [checkpoints] WHERE "
            "TRY_CAST(JSON_VALUE(metadata,'$.created_at') AS DATETIME2) "
            "< DATEADD(DAY,-30,GETUTCDATE()));"
        ),
    }

    print(f"\n  single-thread delete : {result['single_thread_delete_ms']} ms "
          f"({args.turns} turns)")
    print(f"  bulk delete          : {result['bulk_delete_total_ms']} ms "
          f"({rows_bulk} rows)")
    print(f"  per-thread delete    : {result['per_thread_delete_ms']} ms")
    print(f"  per-1k-rows          : {result['per_1k_rows_ms']} ms\n")

    out = save_result("pruning", result)
    print(f"saved -> {out}")
    conn.close()
    saver.pool.close()
    return result


if __name__ == "__main__":
    main()
