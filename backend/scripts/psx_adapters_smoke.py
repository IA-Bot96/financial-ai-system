"""Live reachability smoke test for the PSX external adapters.

Hits each PSX adapter through the real HttpTransport and reports reachable / parsed /
blocked — run this FROM THE DEPLOYED ENVIRONMENT (a Heroku dyno), since PSX may block
datacenter IPs that a local/office IP passes (see SECURITY.md).

    python -m scripts.psx_adapters_smoke [TICKER]   # default MTL

Exit code 0 if all reachable, else 1. Connectivity/shape check only — PSX data
licensing still gates any commercial use.
"""

from __future__ import annotations

import sys

from app.engines.fie.apis import (
    AnalysisReports, ApiClient, CompanyOverview, CompanyPayouts, PSX,
    PSXAnnouncements, Symbols,
)
from app.engines.fie.apis.base import HttpTransport


def _safe(s) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


def _try(name: str, fn) -> bool:
    try:
        n = fn()
        print(f"OK    {name:<22} -> {n}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  {name:<22} -> {type(e).__name__}: {_safe(e)[:120]}")
        return False


def main() -> int:
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MTL"
    client = ApiClient(HttpTransport(), max_retries=1)
    ok = []

    sym = Symbols(client)
    ok.append(_try("symbols_master", lambda: f"{len(sym._load())} symbols; "
                   f"ticker_for('Millat Tractors')={sym.ticker_for('Millat Tractors')}"))
    ok.append(_try("company_overview", lambda: f"{len(CompanyOverview(client).fetch(symbol=ticker).items)} fields"))
    ok.append(_try("company_payouts", lambda: f"{len(CompanyPayouts(client).payouts(symbol=ticker).items)} payouts"))
    ok.append(_try("announcements", lambda: f"{len(PSXAnnouncements(client).recent(symbol=ticker).items)} items"))
    ok.append(_try("psx_quote", lambda: f"{len(PSX(client).quote(ticker).items)} quote rows"))
    ok.append(_try("analysis_reports", lambda: f"{len(AnalysisReports(client)._records(2025))} companies"))

    n_ok = sum(ok)
    print(f"\n{n_ok}/{len(ok)} adapters reachable")
    return 0 if n_ok == len(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
