"""FIE bake-off harness: run a question set through the engine against the REAL configured model,
reporting per-query latency, LLM calls, token usage, answer source, citations, and an optional
expected-substring check.

This is the feasibility instrument: it tells us, on the model we actually ship (gpt-5.4-mini),
whether the engine answers the demo questions and how slow/expensive it is.

Usage:
    python -m tools.fie_bakeoff                      # default question set
    python -m tools.fie_bakeoff --session <path-to-xlsx>
"""

from __future__ import annotations

import argparse
import logging
import re
import time

from app.core.config import get_settings
from app.engines.fie import FinancialIntelligenceEngine, FinancialFactStore
from app.engines.fie.llm import OpenAILLM

_DEFAULT_SESSION = "storage/sessions/0f49d26c1b12468c.xlsx"

# (query, expected substring or None). Expected values are normalized (commas/%/spaces stripped)
# before the check. Includes the demo-failing asks + known-answer queries + an availability + a
# sector query (which has no workbook primitive -> should gracefully say "not available").
QUESTIONS = [
    ("what is gp margin in 2022?", "19.1"),
    ("what is gp margin in 2025?", "26.6"),
    ("what is gp in 2025?", "13867091"),
    ("what was revenue in 2024?", "91534501"),
    ("what is the current ratio in 2024?", None),
    ("return on equity for 2024?", None),
    ("what is net margin in 2023?", None),
    ("what's in this workbook?", None),
    ("is gp % in this workbook?", None),
    ("what is sector gross profit?", None),
]


class _TokenMeter(logging.Handler):
    """Sum prompt/completion tokens + count LLM calls from the engine's own log lines."""
    _RE = re.compile(r"prompt_tokens=(\d+) completion_tokens=(\d+)")

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.reset()

    def reset(self):
        self.calls = 0
        self.ptok = 0
        self.ctok = 0

    def emit(self, record):
        try:
            m = self._RE.search(record.getMessage())
        except Exception:
            return
        if m:
            self.calls += 1
            self.ptok += int(m.group(1))
            self.ctok += int(m.group(2))


def _norm(s: str) -> str:
    return re.sub(r"[,\s%$]", "", (s or "").lower())


def _real_llm():
    s = get_settings()
    key = (s.openai_api_key or "").strip()
    if not key:
        return None
    return OpenAILLM(
        model=s.openai_model, api_key=key,
        max_input_chars=s.llm_max_input_chars, max_output_tokens=s.llm_max_output_tokens,
        json_temperature=s.llm_json_temperature, text_temperature=s.llm_text_temperature,
        seed=s.llm_seed,
    )


def _run_one(engine, query, meter):
    meter.reset()
    t0 = time.perf_counter()
    resp = engine.answer(query)
    dt = time.perf_counter() - t0
    d = resp.model_dump()
    return {
        "latency": dt, "calls": meter.calls, "ptok": meter.ptok, "ctok": meter.ctok,
        "source": d.get("prose_source"), "cited": len(d.get("citations") or []),
        "answer": (d.get("direct_answer") or "").replace("\n", " "),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=_DEFAULT_SESSION)
    args = ap.parse_args()

    logging.disable(logging.NOTSET)
    logging.getLogger("app.engines.fie").setLevel(logging.DEBUG)
    meter = _TokenMeter()
    logging.getLogger("app.engines.fie").addHandler(meter)
    # silence everything else to keep the report clean
    for h in logging.getLogger().handlers:
        h.setLevel(logging.CRITICAL)

    llm = _real_llm()
    if llm is None:
        print("NO OPENAI KEY — cannot run a live bake-off. Set OPENAI_API_KEY.")
        return
    store = FinancialFactStore.from_workbook(args.session)
    engine = FinancialIntelligenceEngine(store, llm=llm)

    rows = []
    print(f"\nModel: {get_settings().openai_model}   Session: {args.session}\n")
    hdr = (f"{'lat(s)':>6} {'calls':>5} {'ptok':>6} {'ctok':>5} {'source':12} {'cite':>4} "
           f"{'exp':>3}  query")
    print(hdr)
    print("-" * len(hdr))
    for query, expect in QUESTIONS:
        r = _run_one(engine, query, meter)
        exp = "" if expect is None else ("ok" if _norm(expect) in _norm(r["answer"]) else "MISS")
        rows.append((query, expect, r, exp))
        print(f"{r['latency']:6.2f} {r['calls']:5d} {r['ptok']:6d} {r['ctok']:5d} "
              f"{str(r['source']):12} {r['cited']:4d} {exp:>3}  {query}")
        print(f"   -> {r['answer'][:150]}")

    print("\n=== summary ===")
    exps = [x for (_q, e, _r, x) in rows if e is not None]
    passed = sum(1 for x in exps if x == "ok")
    print(f"avg_latency={sum(r['latency'] for (_q, _e, r, _x) in rows)/len(rows):.2f}s  "
          f"total_tokens={sum(r['ptok'] + r['ctok'] for (_q, _e, r, _x) in rows)}  "
          f"avg_calls={sum(r['calls'] for (_q, _e, r, _x) in rows)/len(rows):.1f}  "
          f"expected_checks={passed}/{len(exps)}")


if __name__ == "__main__":
    main()
