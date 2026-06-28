"""Benchmark 6/7: connection pool sizing (Azure SQL).

Finds the throughput knee by running a fixed concurrent write load through
AzureSqlSaver instances configured with increasing pool sizes.

Usage:
    python -m benchmarks.connection_pool
    python -m benchmarks.connection_pool --workers 20 --ops 200
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.base import empty_checkpoint

from benchmarks._harness import make_saver, percentiles, save_result

POOL_SIZES = [1, 5, 10, 20, 50]


def _ckpt() -> dict:
    c = empty_checkpoint()
    c["channel_values"] = {"counter": 0}
    c["channel_versions"] = {"counter": f"{1:032}.0"}
    return c


def bench_pool(pool_size: int, workers: int, total_ops: int) -> dict:
    saver = make_saver(pool_size=pool_size)
    lats: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    remaining = [total_ops]

    def worker():
        while True:
            with lock:
                if remaining[0] <= 0:
                    return
                remaining[0] -= 1
            tid = f"pool{pool_size}-{uuid.uuid4()}"
            config = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
            ckpt = _ckpt()
            try:
                t0 = time.perf_counter()
                saver.put(config, ckpt, {"step": 0}, ckpt["channel_versions"])
                lat = (time.perf_counter() - t0) * 1000
                saver.delete_thread(tid)
                with lock:
                    lats.append(lat)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    saver.pool.close()

    if not lats:
        return {"pool_size": pool_size, "workers": workers, "errors": len(errors), "ops": 0}

    pc = percentiles(lats)
    return {
        "pool_size": pool_size,
        "workers": workers,
        "ops": len(lats),
        "errors": len(errors),
        "p50": pc["p50"],
        "p95": pc["p95"],
        "p99": pc["p99"],
        "max": pc["max"],
        "throughput_rps": round(len(lats) / wall, 1) if wall > 0 else 0.0,
        "wall_s": round(wall, 2),
    }


def main():
    ap = argparse.ArgumentParser(description="Azure SQL pool sizing")
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--ops", type=int, default=200)
    args = ap.parse_args()

    results = []
    print(f"\nfixed load: {args.workers} workers, {args.ops} ops")
    print(f"\n{'pool':>6}{'ops':>7}{'p50':>9}{'p95':>9}{'rps':>10}{'errors':>9}")
    print("-" * 50)
    for ps in POOL_SIZES:
        r = bench_pool(ps, args.workers, args.ops)
        results.append(r)
        print(
            f"{ps:>6}{r.get('ops', 0):>7}{r.get('p50', 0):>9.1f}"
            f"{r.get('p95', 0):>9.1f}{r.get('throughput_rps', 0):>10.1f}{r.get('errors', 0):>9}"
        )
    print("(latencies in ms)\n")

    out = save_result("connection_pool", {"args": vars(args), "results": results})
    print(f"saved -> {out}")
    return results


if __name__ == "__main__":
    main()
