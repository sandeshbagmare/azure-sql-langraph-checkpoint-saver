"""Master runner: execute all 7 Azure SQL benchmark suites, aggregate to JSON.

Usage:
    python -m benchmarks.run_all              # default scale (~3-6 min)
    python -m benchmarks.run_all --quick      # fast smoke test (~45 s)
    python -m benchmarks.run_all --stress     # heavy run (~20-30 min)

Each suite also writes its own results/<suite>.json. The combined report is
written to results/FULL_REPORT.json and is the single source the docs read.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks import (
    connection_pool,
    correctness,
    db_size,
    history_depth,
    latency,
    payload_scaling,
    pruning,
)
from benchmarks._harness import azure_conn_str, is_real_azure, save_result


def _argv(*a):
    sys.argv = ["_"] + list(a)


def _run(name, fn, *argv):
    _argv(*argv)
    print(f"\n{'=' * 64}\n  SUITE: {name}\n{'=' * 64}")
    try:
        return {"suite": name, "status": "ok", "result": fn.main()}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return {"suite": name, "status": "error", "error": str(e)}


def main():
    ap = argparse.ArgumentParser(description="Run all Azure SQL benchmarks")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--stress", action="store_true")
    args = ap.parse_args()

    if args.quick:
        cfg = dict(lat_n=30, lat_w=8, corr_t=10, corr_i=3, pay_n=5,
                   hist_max=50, hist_n=5, pool_w=10, pool_ops=60,
                   prune_t=20, prune_turns=5, size_runs=40)
    elif args.stress:
        cfg = dict(lat_n=1000, lat_w=30, corr_t=50, corr_i=5, pay_n=50,
                   hist_max=200, hist_n=20, pool_w=30, pool_ops=400,
                   prune_t=500, prune_turns=20, size_runs=500)
    else:
        cfg = dict(lat_n=300, lat_w=15, corr_t=30, corr_i=5, pay_n=20,
                   hist_max=200, hist_n=10, pool_w=20, pool_ops=200,
                   prune_t=100, prune_turns=20, size_runs=300)

    print(f"target db: {azure_conn_str()}")
    print(f"real azure endpoint: {is_real_azure()}")

    suites = []
    suites.append(_run("latency", latency, f"--n={cfg['lat_n']}", f"--workers={cfg['lat_w']}"))
    suites.append(_run("db_size", db_size, f"--runs={cfg['size_runs']}"))
    suites.append(_run("correctness", correctness,
                       f"--threads={cfg['corr_t']}", f"--invocations={cfg['corr_i']}"))
    suites.append(_run("payload_scaling", payload_scaling, f"--n={cfg['pay_n']}"))
    suites.append(_run("history_depth", history_depth,
                       f"--max-turns={cfg['hist_max']}", f"--n={cfg['hist_n']}"))
    suites.append(_run("connection_pool", connection_pool,
                       f"--workers={cfg['pool_w']}", f"--ops={cfg['pool_ops']}"))
    suites.append(_run("pruning", pruning,
                       f"--threads={cfg['prune_t']}", f"--turns={cfg['prune_turns']}"))

    passed = [s for s in suites if s["status"] == "ok"]
    failed = [s for s in suites if s["status"] == "error"]

    print(f"\n{'=' * 64}")
    print(f"  DONE: {len(passed)}/{len(suites)} suites OK")
    for s in failed:
        print(f"  FAILED: {s['suite']} — {s['error']}")
    print(f"{'=' * 64}")

    combined = {
        "target_db": azure_conn_str().split("DATABASE=")[-1].split(";")[0]
        if "DATABASE=" in azure_conn_str() else "unknown",
        "real_azure_endpoint": is_real_azure(),
        "scale": "quick" if args.quick else "stress" if args.stress else "default",
        "config": cfg,
        "suites": suites,
        "summary": {"total": len(suites), "passed": len(passed), "failed": len(failed)},
    }
    out = save_result("FULL_REPORT", combined)
    print(f"\ncombined report -> {out}")
    return combined


if __name__ == "__main__":
    main()
