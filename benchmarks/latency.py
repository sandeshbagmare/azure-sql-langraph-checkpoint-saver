"""Benchmark 1/7: end-to-end latency & throughput (Azure SQL).

Runs a real 3-node LangGraph graph, persisting every step to Azure SQL,
both sequentially and across a worker pool.

Usage:
    python -m benchmarks.latency
    python -m benchmarks.latency --n 500 --workers 20
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks._harness import (
    build_graph,
    initial_state,
    make_saver,
    percentiles,
    save_result,
)


def _run_once(graph, thread_id: str) -> float:
    config = {"configurable": {"thread_id": thread_id}}
    t0 = time.perf_counter()
    graph.invoke(initial_state(), config)
    return (time.perf_counter() - t0) * 1000


def bench_sequential(saver, n: int) -> dict:
    graph = build_graph(saver)
    lats = [_run_once(graph, f"seq-{uuid.uuid4()}") for _ in range(n)]
    return percentiles(lats)


def bench_concurrent(saver, n: int, workers: int) -> dict:
    graph = build_graph(saver)
    lats: list[float] = [0.0] * n
    errors: list[str] = []
    lock = threading.Lock()
    idx_iter = iter(range(n))

    def worker():
        while True:
            with lock:
                try:
                    i = next(idx_iter)
                except StopIteration:
                    return
            try:
                lats[i] = _run_once(graph, f"conc-{uuid.uuid4()}")
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(str(e))
                lats[i] = -1.0

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    good = [x for x in lats if x >= 0]
    stats = percentiles(good)
    # Wall-clock throughput is more honest for concurrent runs
    stats["throughput_rps"] = round(len(good) / wall, 1) if wall > 0 else 0.0
    stats["errors"] = len(errors)
    stats["wall_s"] = round(wall, 2)
    return stats


def main():
    ap = argparse.ArgumentParser(description="Azure SQL latency benchmark")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    saver = make_saver(pool_size=max(args.workers * 2, 20))

    results = {}
    print(f"\n[latency] sequential n={args.n} ...")
    results[f"sequential n={args.n}"] = bench_sequential(saver, args.n)

    print(f"[latency] concurrent n={args.n} workers={args.workers} ...")
    results[f"concurrent n={args.n} w={args.workers}"] = bench_concurrent(
        saver, args.n, args.workers
    )

    # Header
    print(
        f"\n{'Scenario':<34}{'n':>6}{'mean':>8}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}{'rps':>9}"
    )
    print("-" * 89)
    for label, s in results.items():
        print(
            f"{label:<34}{s['n']:>6}{s['mean']:>8.1f}{s['p50']:>8.1f}"
            f"{s['p95']:>8.1f}{s['p99']:>8.1f}{s['max']:>8.1f}{s['throughput_rps']:>9.1f}"
        )
    print("(latencies in ms)\n")

    out = save_result("latency", {"args": vars(args), "results": results})
    print(f"saved -> {out}")
    saver.pool.close()
    return results


if __name__ == "__main__":
    main()
