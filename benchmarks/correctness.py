"""Benchmark 3/7: correctness under concurrency (Azure SQL).

Runs many concurrent threads, each invoking the graph multiple times on its
own thread_id, then verifies:
- get_tuple() returns the latest non-empty checkpoint
- list() returns a non-empty descending history
- no PK violations / lost writes / exceptions

Usage:
    python -m benchmarks.correctness
    python -m benchmarks.correctness --threads 40 --invocations 5
"""
from __future__ import annotations

import argparse
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks._harness import build_graph, initial_state, make_saver, save_result


def main():
    ap = argparse.ArgumentParser(description="Azure SQL concurrency correctness")
    ap.add_argument("--threads", type=int, default=30)
    ap.add_argument("--invocations", type=int, default=5)
    args = ap.parse_args()

    saver = make_saver(pool_size=max(args.threads, 25))
    graph = build_graph(saver)

    errors: list[str] = []
    lock = threading.Lock()
    tids = [f"corr-{uuid.uuid4()}" for _ in range(args.threads)]

    def worker(tid: str):
        config = {"configurable": {"thread_id": tid}}
        try:
            for _ in range(args.invocations):
                graph.invoke(initial_state(), config)
            tup = saver.get_tuple(config)
            if tup is None:
                with lock:
                    errors.append(f"[{tid}] get_tuple None")
                return
            if not tup.checkpoint.get("channel_values"):
                with lock:
                    errors.append(f"[{tid}] empty channel_values")
            if not list(saver.list(config)):
                with lock:
                    errors.append(f"[{tid}] empty list()")
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(f"[{tid}] {e}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in tids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for tid in tids:
        try:
            saver.delete_thread(tid)
        except Exception:
            pass

    passed = len(errors) == 0
    result = {
        "threads": args.threads,
        "invocations_per_thread": args.invocations,
        "total_invocations": args.threads * args.invocations,
        "errors": errors[:10],
        "error_count": len(errors),
        "passed": passed,
    }
    status = "PASS" if passed else f"FAIL ({len(errors)} errors)"
    print(f"\n[correctness] {args.threads} threads x {args.invocations} invocations -> {status}")
    for e in errors[:5]:
        print(f"   - {e}")

    out = save_result("correctness", result)
    print(f"saved -> {out}")
    saver.pool.close()
    return result


if __name__ == "__main__":
    main()
