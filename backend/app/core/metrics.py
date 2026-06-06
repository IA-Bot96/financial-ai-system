"""Minimal in-process metrics (no external dependency).

A thread-safe counter + latency-summary registry exposed in the Prometheus text
exposition format at ``/metrics``. This is intentionally tiny — enough to monitor
query volume, intent mix, error/degradation/claim-drop rates, and latency — without
pulling in a metrics client or a backing store. (Per-process: aggregate across dynos
with a scraper that sums instances, or swap for prometheus_client later.)
"""

from __future__ import annotations

import threading
from typing import Optional


def _fmt_labels(labels: Optional[dict]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_esc(str(v))}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Metrics:
    """Counters and latency summaries, keyed by (name, sorted-labels)."""

    def __init__(self) -> None:
        self._counters: dict[tuple, float] = {}
        self._sum: dict[tuple, float] = {}     # latency sum (seconds)
        self._cnt: dict[tuple, int] = {}       # latency observation count
        self._lock = threading.Lock()

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe_latency(self, name: str, seconds: float, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._sum[key] = self._sum.get(key, 0.0) + seconds
            self._cnt[key] = self._cnt.get(key, 0) + 1

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            sums, cnts = dict(self._sum), dict(self._cnt)
        for (name, labels), val in sorted(counters.items()):
            lines.append(f"{name}{_fmt_labels(dict(labels))} {val}")
        for (name, labels), s in sorted(sums.items()):
            lbl = _fmt_labels(dict(labels))
            lines.append(f"{name}_sum{lbl} {s}")
            lines.append(f"{name}_count{lbl} {cnts.get((name, labels), 0)}")
        return "\n".join(lines) + ("\n" if lines else "")


# process-wide singleton
METRICS = Metrics()
