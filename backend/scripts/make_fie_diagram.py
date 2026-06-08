"""Generate a presentation SVG of the FIE pipeline (workbook + question -> cited answer).

Run:  python -m scripts.make_fie_diagram [out.svg]
Default out: the user's Desktop.
"""
from __future__ import annotations

import sys
from pathlib import Path

# ---- theme (matches the OCR pipeline diagram) ------------------------------
BG = "#f1f5f9"
CARD = "#ffffff"
CARD_STROKE = "#e2e8f0"
H1 = "#0f172a"
BODY = "#475569"
MUTE = "#94a3b8"
CHIP_BG = "#f1f5f9"
CHIP_TX = "#0f172a"
ARROW = "#94a3b8"
PILL_BG = "#1e293b"
PILL_TX = "#e2e8f0"
FONT = "Segoe UI, Roboto, Helvetica, Arial, sans-serif"
MONO = "Consolas, 'Courier New', monospace"

KIND = {
    "rule":   {"stripe": "#0d9488", "tx": "#0f766e", "bg": "#ccfbf1"},
    "llm":    {"stripe": "#7c3aed", "tx": "#6d28d9", "bg": "#ede9fe"},
    "hybrid": {"stripe": "#d97706", "tx": "#b45309", "bg": "#fef3c7"},
}

LAYERS = [
    dict(n=1, name="Session Ingest", kind="rule", badge="Rule-Based",
         desc="Parse the uploaded workbook once into a queryable fact store.",
         inp=".xlsx workbook", out="FactStore + metrics",
         bul=["Excel → long-format facts (statement, year, value, cell)",
              "Classify each sheet (statement / detail / ledger / insights)",
              "Read source & validation ledgers → per-fact provenance"]),
    dict(n=2, name="Query Understanding", kind="hybrid", badge="Rules + LLM",
         desc="Turn the natural-language question into a structured intent.",
         inp="Question + history", out="QueryFrame",
         bul=["Rules extract intent, metric(s), year, formula, company",
              "Ontology maps query terms → canonical metrics (aliases)",
              'LLM resolves follow-ups ("and 2024?") from chat history']),
    dict(n=3, name="Planning & Source Routing", kind="rule", badge="Rule-Based",
         desc="Decide which sources to consult for this intent.",
         inp="QueryFrame", out="SourcePlan",
         bul=["Route to workbook (internal) and/or external APIs",
              "Pick sources by intent (news, PSX, dividends, forecasts…)",
              "Shortlist relevant APIs from the registry; budget-capped"]),
    dict(n=4, name="Retrieval — Internal + External", kind="hybrid", badge="Workbook + APIs",
         desc="Gather cited evidence from the workbook and the web.",
         inp="SourcePlan", out="EvidenceItem[]",
         bul=["Workbook lookup → facts with citations (cell / page)",
              "External APIs: news, PSX prices, dividends, forecasts",
              "Resilient HTTP: retry · circuit-breaker · cache · SSRF guard"]),
    dict(n=5, name="Calculation & Intent Handlers", kind="hybrid", badge="Rule + LLM agent",
         desc="Compute ratios and run the intent-specific handler.",
         inp="Frame + Evidence", out="Calcs + ctx",
         bul=["Formula engine: ratios, margins, growth, EBITDA, FCF…",
              "13 intent handlers (lookup, ratio, trend, overview, risk…)",
              "LLM agent tool-composer when intent unknown / no facts"]),
    dict(n=6, name="Evidence Trust & Conflicts", kind="hybrid", badge="Rule + Embeddings",
         desc="Rank, role-tag, and reconcile the evidence.",
         inp="Calcs + ctx", out="Roled evidence + Conflicts",
         bul=["News: chunk → embed (BGE) → rank vs query → dedup",
              "Admission: tag each item baseline / supporting / forecast…",
              "Conflict detection + authority matrix (internal > external)"]),
    dict(n=7, name="Confidence & Response", kind="hybrid", badge="Template + LLM",
         desc="Score the answer, then render it with citations.",
         inp="Roled evidence + Conflicts", out="Response",
         bul=["Confidence: weakest-link score → High / Medium / Low",
              "7-section answer; every finding must carry a citation [Cn]",
              "LLM narration over the reasoning graph (numeric-guarded)"]),
    dict(n=8, name="Trace & Answer", kind="rule", badge="Rule-Based",
         desc="Persist the reasoning trail and return the answer.",
         inp="Response + artifacts", out="Answer + trace.json",
         bul=["Persist TraceRecord (frame, plan, evidence, response)",
              "Return: direct answer · findings · analysis · confidence",
              "Echo the frame so the next follow-up has context"]),
]

ARTIFACTS = [".xlsx workbook", "FactStore + metrics", "QueryFrame", "SourcePlan",
             "EvidenceItem[]", "Calcs + ctx", "Roled evidence + Conflicts", "Response",
             "Answer  +  trace.json"]

# ---- geometry --------------------------------------------------------------
W = 1340
CARD_X, CARD_W, CARD_H = 190, 960, 212
GAP = 86
Y0 = 330
STEP = CARD_H + GAP
CX = W // 2
H = Y0 + STEP * len(LAYERS) + 170


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path.home() / "Desktop" / "AI Financial Intelligence - FIE Pipeline.svg"
    s: list[str] = []
    s.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="{FONT}">')
    s.append('<defs>'
             '<filter id="sh" x="-20%" y="-20%" width="140%" height="140%">'
             '<feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="#0f172a" flood-opacity="0.10"/>'
             '</filter>'
             '<marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
             'markerHeight="7" orient="auto-start-reverse">'
             f'<path d="M0 0 L10 5 L0 10 z" fill="{ARROW}"/></marker></defs>')
    s.append(f'<rect width="{W}" height="{H}" fill="{BG}"/>')

    s.append(f'<text x="{CX}" y="56" text-anchor="middle" font-size="30" font-weight="700" '
             f'fill="{H1}">Financial Intelligence Engine (FIE) &#8212; Question to Answer</text>')
    s.append(f'<text x="{CX}" y="86" text-anchor="middle" font-size="15" fill="{BODY}">'
             f'How a loaded workbook and a natural-language question become a cited, '
             f'confidence-rated answer</text>')

    # legend
    for k, lab, lx in [("rule", "Rule-Based", CX - 300), ("hybrid", "Hybrid · LLM + Rule", CX - 100),
                       ("llm", "LLM (GPT)", CX + 140)]:
        c = KIND[k]
        s.append(f'<rect x="{lx}" y="108" width="16" height="16" rx="4" fill="{c["stripe"]}"/>')
        s.append(f'<text x="{lx+24}" y="121" font-size="13" fill="{BODY}">{esc(lab)}</text>')

    # input node
    iw, ih, iy = 540, 80, 164
    ix = CX - iw // 2
    s.append(f'<rect x="{ix}" y="{iy}" width="{iw}" height="{ih}" rx="14" fill="#eff6ff" '
             f'stroke="#2563eb" stroke-width="1.5" filter="url(#sh)"/>')
    s.append(f'<text x="{CX}" y="{iy+31}" text-anchor="middle" font-size="17" font-weight="700" '
             f'fill="#1e3a8a">INPUT &#8212; Workbook session (.xlsx)  +  a question</text>')
    s.append(f'<text x="{CX}" y="{iy+55}" text-anchor="middle" font-size="12.5" fill="#1d4ed8">'
             f'workbook loads once (Layer 1); each question runs Layers 2&#8211;8</text>')

    prev_bottom = iy + ih
    for i, L in enumerate(LAYERS):
        cy = Y0 + i * STEP
        c = KIND[L["kind"]]
        _connector(s, prev_bottom, cy, ARTIFACTS[i])
        prev_bottom = cy + CARD_H

        s.append(f'<rect x="{CARD_X}" y="{cy}" width="{CARD_W}" height="{CARD_H}" rx="16" '
                 f'fill="{CARD}" stroke="{CARD_STROKE}" stroke-width="1.5" filter="url(#sh)"/>')
        s.append(f'<rect x="{CARD_X}" y="{cy}" width="9" height="{CARD_H}" rx="4" fill="{c["stripe"]}"/>')
        ncx = CARD_X + 42
        s.append(f'<circle cx="{ncx}" cy="{cy+40}" r="19" fill="{c["stripe"]}"/>')
        s.append(f'<text x="{ncx}" y="{cy+47}" text-anchor="middle" font-size="19" '
                 f'font-weight="700" fill="#ffffff">{L["n"]}</text>')
        s.append(f'<text x="{CARD_X+74}" y="{cy+37}" font-size="20" font-weight="700" '
                 f'fill="{H1}">{esc(L["name"])}</text>')
        s.append(f'<text x="{CARD_X+74}" y="{cy+62}" font-size="13.5" fill="{BODY}">'
                 f'{esc(L["desc"])}</text>')
        bw = int(len(L["badge"]) * 7.2 + 26)
        bx = CARD_X + CARD_W - 20 - bw
        s.append(f'<rect x="{bx}" y="{cy+22}" width="{bw}" height="26" rx="13" fill="{c["bg"]}"/>')
        s.append(f'<text x="{bx+bw//2}" y="{cy+39}" text-anchor="middle" font-size="12.5" '
                 f'font-weight="600" fill="{c["tx"]}">{esc(L["badge"])}</text>')
        s.append(f'<line x1="{CARD_X+24}" y1="{cy+78}" x2="{CARD_X+CARD_W-22}" y2="{cy+78}" '
                 f'stroke="{CARD_STROKE}" stroke-width="1.2"/>')
        col_in_x, col_mid_x, col_out_x = CARD_X + 30, CARD_X + 280, CARD_X + 712
        s.append(f'<line x1="{CARD_X+262}" y1="{cy+94}" x2="{CARD_X+262}" y2="{cy+CARD_H-16}" '
                 f'stroke="{CARD_STROKE}" stroke-width="1"/>')
        s.append(f'<line x1="{CARD_X+696}" y1="{cy+94}" x2="{CARD_X+696}" y2="{cy+CARD_H-16}" '
                 f'stroke="{CARD_STROKE}" stroke-width="1"/>')
        _coltitle(s, col_in_x, cy + 104, "INPUT")
        _chip(s, col_in_x, cy + 114, L["inp"], 222)
        _coltitle(s, col_mid_x, cy + 104, "INTERNAL")
        for j, b in enumerate(L["bul"]):
            yb = cy + 128 + j * 22
            s.append(f'<circle cx="{col_mid_x+3}" cy="{yb-4}" r="2.4" fill="{c["stripe"]}"/>')
            s.append(f'<text x="{col_mid_x+14}" y="{yb}" font-size="12.7" fill="{H1}">{esc(b)}</text>')
        _coltitle(s, col_out_x, cy + 104, "OUTPUT")
        _chip(s, col_out_x, cy + 114, L["out"], 222)

    ow, oh = 560, 104
    oy = prev_bottom + GAP
    _connector(s, prev_bottom, oy, ARTIFACTS[-1])
    ox = CX - ow // 2
    s.append(f'<rect x="{ox}" y="{oy}" width="{ow}" height="{oh}" rx="14" fill="#f0fdf4" '
             f'stroke="#16a34a" stroke-width="1.5" filter="url(#sh)"/>')
    s.append(f'<text x="{CX}" y="{oy+30}" text-anchor="middle" font-size="17" font-weight="700" '
             f'fill="#14532d">OUTPUT &#8212; Cited answer</text>')
    s.append(f'<text x="{CX}" y="{oy+54}" text-anchor="middle" font-size="12.5" fill="#15803d">'
             f'direct answer · key findings [with citations] · supporting analysis · calculations</text>')
    s.append(f'<text x="{CX}" y="{oy+74}" text-anchor="middle" font-size="12.5" fill="#15803d">'
             f'confidence band (High / Med / Low) · exposed conflicts · coverage</text>')
    s.append(f'<text x="{CX}" y="{oy+93}" text-anchor="middle" font-size="12" fill="#166534">'
             f'sidecar trace.json: full reasoning trail (frame · plan · evidence · response)</text>')

    fy = oy + oh + 36
    s.append(f'<text x="{CX}" y="{fy}" text-anchor="middle" font-size="12.5" fill="{MUTE}">'
             f'Cross-cutting&#160;&#160;·&#160;&#160;conversation memory (follow-ups)&#160;&#160;·'
             f'&#160;&#160;citation enforcement&#160;&#160;·&#160;&#160;contract checks (bootcheck)'
             f'&#160;&#160;·&#160;&#160;degradation handling&#160;&#160;·&#160;&#160;reasoning trace</text>')

    s.append('</svg>')
    out.write_text("\n".join(s), encoding="utf-8")
    print("Wrote", out)


def _connector(s, from_bottom, to_top, label):
    s.append(f'<line x1="{CX}" y1="{from_bottom}" x2="{CX}" y2="{to_top}" stroke="{ARROW}" '
             f'stroke-width="2" marker-end="url(#arr)"/>')
    pw = int(len(label) * 7.0 + 30)
    px = CX - pw // 2
    pcy = (from_bottom + to_top) // 2
    s.append(f'<rect x="{px}" y="{pcy-13}" width="{pw}" height="26" rx="13" fill="{PILL_BG}"/>')
    s.append(f'<text x="{CX}" y="{pcy+5}" text-anchor="middle" font-size="12.5" '
             f'font-family="{MONO}" fill="{PILL_TX}">{esc(label)}</text>')


def _coltitle(s, x, y, t):
    s.append(f'<text x="{x}" y="{y}" font-size="10.5" font-weight="700" letter-spacing="1.2" '
             f'fill="{MUTE}">{esc(t)}</text>')


def _chip(s, x, y, t, maxw):
    w = min(maxw, int(len(t) * 6.7 + 18))
    s.append(f'<rect x="{x}" y="{y}" width="{w}" height="24" rx="6" fill="{CHIP_BG}" '
             f'stroke="{CARD_STROKE}" stroke-width="1"/>')
    s.append(f'<text x="{x+9}" y="{y+16}" font-size="12.3" font-family="{MONO}" '
             f'fill="{CHIP_TX}">{esc(t)}</text>')


if __name__ == "__main__":
    main()
