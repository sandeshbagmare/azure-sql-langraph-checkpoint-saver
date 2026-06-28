"""Benchmark 4/7: payload size scaling (Azure SQL).

Measures put() and get_tuple() latency as the checkpoint channel payload
grows from 1 KB to 1 MB. Uses the real saver API (not raw SQL) so the cost
of serialization + VARBINARY(MAX) IO is included.

Usage:
    python -m benchmarks.payload_scaling
    python -m benchmarks.payload_scaling --n 25
"""
from __future__ import annotations

import argparse
import random
import statistics
import string
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.base import empty_checkpoint

from benchmarks._harness import make_saver, percentiles, save_result

PAYLOAD_SIZES_KB = [1, 10, 50, 100, 500, 1024]


def _checkpoint_with_payload(idx: int, text: str) -> dict:
    c = empty_checkpoint()
    c["channel_values"] = {"text": text, "counter": idx}
    c["channel_versions"] = {"text": f"{idx + 1:032}.0", "counter": f"{idx + 1:032}.1"}
    return c


def bench_size(saver, payload_kb: int, n: int) -> dict:
    text = "".join(random.choices(string.ascii_letters + " \n", k=payload_kb * 1024))
    put_lat: list[float] = []
    get_lat: list[float] = []
    tid = f"payload-{payload_kb}kb-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}

    for i in range(n):
        ckpt = _checkpoint_with_payload(i, text)
        t0 = time.perf_counter()
        cfg = saver.put(config, ckpt, {"step": i}, ckpt["channel_versions"])
        put_lat.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        saver.get_tuple(cfg)
        get_lat.append((time.perf_counter() - t0) * 1000)

    saver.delete_thread(tid)

    return {
        "payload_kb": payload_kb,
        "n": n,
        "put_p50": round(statistics.median(put_lat), 2),
        "put_p95": percentiles(put_lat)["p95"],
        "put_max": round(max(put_lat), 2),
        "get_p50": round(statistics.median(get_lat), 2),
        "get_p95": percentiles(get_lat)["p95"],
        "get_max": round(max(get_lat), 2),
        "put_rps": round(n / (sum(put_lat) / 1000), 1),
    }


def main():
    ap = argparse.ArgumentParser(description="Azure SQL payload scaling")
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()

    saver = make_saver(pool_size=10)
    results = []

    print(f"\n{'payload':>9}{'put p50':>10}{'put p95':>10}{'get p50':>10}{'get p95':>10}{'put rps':>10}")
    print("-" * 59)
    for kb in PAYLOAD_SIZES_KB:
        r = bench_size(saver, kb, args.n)
        results.append(r)
        print(
            f"{kb:>6} KB{r['put_p50']:>10.2f}{r['put_p95']:>10.2f}"
            f"{r['get_p50']:>10.2f}{r['get_p95']:>10.2f}{r['put_rps']:>10.1f}"
        )
    print("(latencies in ms)\n")

    out = save_result("payload_scaling", {"args": vars(args), "results": results})
    print(f"saved -> {out}")
    saver.pool.close()
    return results


if __name__ == "__main__":
    main()
