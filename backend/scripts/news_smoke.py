"""Live smoke test for the configured news provider keys.

Reads keys from backend/.env (via Settings), then makes one real request per
provider using the SAME build/parse logic the engine uses, and reports:
  status code, parsed-article count, and a body snippet on failure.

Run:  python -m scripts.news_smoke      (from backend/, with .env populated)
This makes real network calls; it is a dev tool, not part of the test suite.
"""

from __future__ import annotations

import json

import httpx

from app.core.config import get_settings
from app.engines.fie.apis.news_providers import PROVIDERS


def main() -> None:
    s = get_settings()
    print(f"{'provider':<14} {'key':<5} {'http':<6} {'arts':<5} note")
    print("-" * 72)
    for p in PROVIDERS:
        key = (getattr(s, p.key_setting, "") or "").strip()
        if not key:
            print(f"{p.id:<14} {'no':<5} {'-':<6} {'-':<5} (no key set — skipped)")
            continue

        # ticker-only feeds need a symbol; others use a keyword that always hits
        if p.requires_symbol:
            path, params = p.build(query="apple", symbol="AAPL", key=key,
                                   limit=5, anchor_date=None)
        else:
            path, params = p.build(query="stock market", symbol=None, key=key,
                                   limit=5, anchor_date=None)
        url = p.base_url.rstrip("/") + "/" + path.lstrip("/")

        try:
            r = httpx.get(url, params=params, timeout=20.0)
        except Exception as exc:  # noqa: BLE001
            print(f"{p.id:<14} {'yes':<5} {'ERR':<6} {'-':<5} {type(exc).__name__}: {exc}")
            continue

        note, n = "", "-"
        if r.status_code == 200:
            try:
                arts = p.parse(r.json())
                n = len(arts)
                if arts:
                    note = f'OK e.g. "{(arts[0].title or "")[:48]}"'
                else:
                    # 200 but nothing parsed — show a hint of the body (often an error/quota msg)
                    note = "OK but 0 parsed: " + json.dumps(r.json())[:120]
            except Exception as exc:  # noqa: BLE001
                note = f"200 but non-JSON/parse error: {exc}"
        else:
            body = r.text.replace("\n", " ")
            note = body[:140]
        note = note.encode("ascii", "replace").decode("ascii")  # console-safe
        print(f"{p.id:<14} {'yes':<5} {str(r.status_code):<6} {str(n):<5} {note}")


if __name__ == "__main__":
    main()
