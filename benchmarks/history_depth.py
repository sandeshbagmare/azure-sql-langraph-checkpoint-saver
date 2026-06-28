"""Benchmark 5/7: history depth scaling (Azure SQL).

Does get_tuple() (latest) stay O(log N) as a thread accumulates checkpoints,
while list() (full scan) grows O(N)? Measures both at increasing depths.

Usage:
    python -m benchmarks.history_depth
    python -m benchmarks.history_depth --max-turns 200 --n 10
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.base import empty_checkpoint

from benchmarks._harness import make_saver, percentiles, save_result

DEPTH_STEPS = [1, 5, 10, 25, 50, 100, 200]


def _ckpt(idx: int) -> dict:
    c = empty_checkpoint()
    c["channel_values"] = {"counter": idx, "data": f"value_{idx}"}
    c["channel_versions"] = {"counter": f"{idx + 1:032}.0", "data": f"{idx + 1:032}.1"}
    return c


def bench_depth(saver, depth: int, n_queries: int) -> dict:
    get_lat: list[float] = []
    list_lat: list[float] = []
    tids: list[str] = []

    for _ in range(n_queries):
        tid = f"hist-{uuid.uuid4()}"
        tids.append(tid)
        config = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
        last_cfg = config
        for i in range(depth):
            ckpt = _ckpt(i)
            last_cfg = saver.put(config, ckpt, {"step": i}, ckpt["channel_versions"])

        t0 = time.perf_counter()
        saver.get_tuple(last_cfg)
        get_lat.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        _ = list(saver.list(config))
        list_lat.append((time.perf_counter() - t0) * 1000)

    for tid in tids:
        saver.delete_thread(tid)

    return {
        "depth": depth,
        "n_queries": n_queries,
        "get_p50": round(statistics.median(get_lat), 2),
        "get_p95": percentiles(get_lat)["p95"],
        "list_p50": round(statistics.median(list_lat), 2),
        "list_p95": percentiles(list_lat)["p95"],
    }


def main():
    ap = argparse.ArgumentParser(description="Azure SQL history depth scaling")
    ap.add_argument("--max-turns", type=int, default=200)
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()

    saver = make_saver(pool_size=10)
    steps = [d for d in DEPTH_STEPS if d <= args.max_turns]
    if args.max_turns not in steps:
        steps.append(args.max_turns)

    results = []
    print(f"\n{'depth':>7}{'get p50':>10}{'get p95':>10}{'list p50':>11}{'list p95':>11}")
    print("-" * 49)
    for depth in steps:
        r = bench_depth(saver, depth, args.n)
        results.append(r)
        print(
            f"{depth:>7}{r['get_p50']:>10.2f}{r['get_p95']:>10.2f}"
            f"{r['list_p50']:>11.2f}{r['list_p95']:>11.2f}"
        )
    print("(latencies in ms)\n")

    out = save_result("history_depth", {"args": vars(args), "results": results})
    print(f"saved -> {out}")
    saver.pool.close()
    return results


if __name__ == "__main__":
    main()
