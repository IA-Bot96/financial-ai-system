"""Latency profiler for the FIE engine (measure-first, per the review's perf item).

Runs the fixed regression-corpus queries against a delivered workbook N times and
reports per-query and aggregate latency (mean / p50 / p95). Use it to decide whether
any caching/optimization is *warranted* before adding it.

    python -m scripts.fie_profile [REPS]      # default 20

Deterministic path only (NullLLM, no external adapters) — isolates the engine's own
compute. Wire an LLM / adapters to profile those paths.
"""

from __future__ import annotations

import os
import statistics
import sys
import time

from app.engines.fie import FinancialFactStore, FinancialIntelligenceEngine
from app.engines.fie.regression_corpus import CASES

_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")


def _pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))]


def main() -> int:
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    if not os.path.exists(_WB):
        print(f"workbook not found: {_WB}")
        return 1

    t_load = time.monotonic()
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB))
    print(f"store+engine load: {(time.monotonic() - t_load) * 1000:.0f} ms\n")

    all_times: list[float] = []
    print(f"{'intent':<22}{'mean ms':>10}{'p50':>8}{'p95':>8}")
    for case in CASES:
        times: list[float] = []
        for _ in range(reps):
            t0 = time.monotonic()
            eng.answer(case.query)
            times.append((time.monotonic() - t0) * 1000)
        all_times += times
        print(f"{case.intent:<22}{statistics.fmean(times):>10.1f}"
              f"{_pct(times, 50):>8.1f}{_pct(times, 95):>8.1f}")

    print(f"\noverall ({len(all_times)} runs): mean={statistics.fmean(all_times):.1f}ms "
          f"p50={_pct(all_times, 50):.1f}ms p95={_pct(all_times, 95):.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
