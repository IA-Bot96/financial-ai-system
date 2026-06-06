"""Live smoke test for the analysis_reports endpoint.

Fetches the real PSX yearly analysis report via the AnalysisReports adapter, parses
it, and prints a sample company's per-metric cited evidence. Network required.

    python -m scripts.analysis_reports_smoke 2025 MTL

NOTE: PSX data carries an "Unauthorized Use of PSX Data" notice — this is a
developer connectivity/shape check only.
"""

from __future__ import annotations

import sys

from app.engines.fie.apis import AnalysisReports, ApiClient
from app.engines.fie.apis.base import HttpTransport


def _safe(s: str) -> str:
    return s.encode("ascii", "replace").decode("ascii")


def main() -> int:
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    symbol = sys.argv[2] if len(sys.argv) > 2 else "MTL"

    ar = AnalysisReports(ApiClient(HttpTransport(), max_retries=1))
    url = ar.spec.base_url.rstrip("/") + "/" + ar.spec.path.format(year=year)
    print(f"GET {url}")

    try:
        records = ar._records(year)               # triggers the live fetch + parse
    except Exception as e:                          # noqa: BLE001
        print(f"FAIL  fetch/parse error: {type(e).__name__}: {e}")
        return 1

    if not records:
        print("FAIL  no records parsed (endpoint reachable but empty/blocked?)")
        return 1
    print(f"OK    parsed {len(records)} company records for FY{year}")

    rec = records.get(symbol)
    if not rec:
        sample = list(records)[:8]
        print(f"WARN  {symbol} not found; sample symbols: {sample}")
        return 0

    print(f"\n{symbol}: {_safe(str(rec.get('name')))}  sector={_safe(str(rec.get('sector')))}"
          f"  year_end={rec.get('year_end')}")
    res = ar.facts_for(year, symbol=symbol)
    print(f"per-metric cited evidence ({len(res.items)}):")
    for it in res.items:
        loc = it.citations[0].locator
        print(f"  - {loc['metric']:<20} = {it.value:>14,.3f} {it.unit}   "
              f"[{_safe(it.citations[0].display)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
