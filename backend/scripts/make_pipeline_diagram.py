"""Generate a presentation SVG of the NEW OCR extraction pipeline (PDF(s) -> .xlsx).

Run:  python -m scripts.make_pipeline_diagram [out.svg]
Default out: the user's Desktop next to the old diagram.
"""
from __future__ import annotations

import sys
from pathlib import Path

# ---- theme -----------------------------------------------------------------
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

# ---- pipeline content (the actual layers) ----------------------------------
LAYERS = [
    dict(n=1, name="Ingestion / OCR", kind="rule", badge="Rule-Based",
         desc="Read every page; OCR the scanned ones.",
         inp="PDF(s)", out="IngestedDoc",
         bul=["PyMuPDF extracts native text per page",
              "Image-only pages → Tesseract OCR",
              "Parallel page+doc pool (serial when packaged)"]),
    dict(n=2, name="Table & Section Detection", kind="rule", badge="Rule-Based + Embeddings",
         desc="Locate tables, classify statement type, find narrative.",
         inp="IngestedDoc", out="TableSet + sections",
         bul=["Word-grid clustering → table regions",
              "Statement-type classifier (fuzzy + local embeddings)",
              "Narrative sections (CEO review, MD&A, outlook)"]),
    dict(n=3, name="Interpretation", kind="hybrid", badge="LLM + Rule-Based",
         desc="Turn pages into structured financial tables + insights.",
         inp="IngestedDoc + TableSet", out="DocumentResult",
         bul=["GPT → strict JSON tables (values, roles, components)",
              "Vision option: page image + text  ·  gpt-5.4-mini",
              "Rule cell-normalize  +  GPT narrative insights"]),
    dict(n=4, name="Multi-Year Resolution", kind="rule", badge="Rule-Based",
         desc="Merge all reports into one multi-year view.",
         inp="DocumentResult ×N", out="CompanyResult",
         bul=["Merge same statements across years & reports",
              "Consolidate split leaves · dedup · restatements",
              "Keep file/page provenance on every value"]),
    dict(n=5, name="Face Truth & Template Mapping", kind="rule", badge="Rule-Based + Embeddings",
         desc="Pick the audited truth; map values into the template.",
         inp="CompanyResult (+ Template.xlsx)", out="Workbook + Plan",
         bul=["Face truth per (metric, year) + accounting-identity checks",
              "Template: label-match (fuzzy + embeddings + family gate)",
              "No template → one styled sheet per table"]),
    dict(n=6, name="Assembly, Validation & Export", kind="rule", badge="Rule-Based",
         desc="Repair, reconcile, validate, and write the workbook.",
         inp="Workbook + Plan", out=".xlsx + manifest",
         bul=["Formula repair + headline override to audited truth",
              "Materiality-gated reconciliation + validation/source ledgers",
              "Recalc + embed sheet→PDF-page map (provenance)"]),
]

# artifact label on the arrow INTO each layer (and after the last one)
ARTIFACTS = ["PDF pages", "IngestedDoc", "TableSet + sections",
             "DocumentResult ×N", "CompanyResult", "Workbook + Plan + FaceTruth",
             ".xlsx  +  manifest.json"]

# ---- geometry --------------------------------------------------------------
W = 1340
CARD_X, CARD_W, CARD_H = 190, 960, 212
GAP = 86
Y0 = 312                                   # first card top
STEP = CARD_H + GAP
CX = W // 2                                 # flow centre line
H = Y0 + STEP * len(LAYERS) + 150


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card_y(i: int) -> int:
    return Y0 + i * STEP


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path.home() / "Desktop" / "AI Financial Intelligence - OCR Pipeline (new).svg"
    s: list[str] = []
    s.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="{FONT}">')
    s.append('<defs>'
             '<filter id="sh" x="-20%" y="-20%" width="140%" height="140%">'
             '<feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="#0f172a" flood-opacity="0.10"/>'
             '</filter>'
             '<marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
             'markerHeight="7" orient="auto-start-reverse">'
             f'<path d="M0 0 L10 5 L0 10 z" fill="{ARROW}"/></marker>'
             '</defs>')
    s.append(f'<rect width="{W}" height="{H}" fill="{BG}"/>')

    # ---- title ----
    s.append(f'<text x="{CX}" y="58" text-anchor="middle" font-size="30" font-weight="700" '
             f'fill="{H1}">OCR Extraction Pipeline &#8212; PDF(s) to Excel</text>')
    s.append(f'<text x="{CX}" y="88" text-anchor="middle" font-size="15" fill="{BODY}">'
             f'How annual-report PDFs are converted into a structured, audited .xlsx workbook</text>')

    # ---- legend ----
    leg = [("rule", "Rule-Based"), ("hybrid", "Hybrid · LLM + Rule"), ("llm", "LLM (GPT)")]
    lx = CX - 300
    for k, lab in leg:
        c = KIND[k]
        s.append(f'<rect x="{lx}" y="112" width="16" height="16" rx="4" fill="{c["stripe"]}"/>')
        s.append(f'<text x="{lx+24}" y="125" font-size="13" fill="{BODY}">{esc(lab)}</text>')
        lx += 200

    # ---- input node ----
    iw, ih, iy = 360, 76, 168
    ix = CX - iw // 2
    s.append(f'<rect x="{ix}" y="{iy}" width="{iw}" height="{ih}" rx="14" fill="#eff6ff" '
             f'stroke="#2563eb" stroke-width="1.5" filter="url(#sh)"/>')
    s.append(f'<text x="{CX}" y="{iy+31}" text-anchor="middle" font-size="17" font-weight="700" '
             f'fill="#1e3a8a">INPUT &#8212; Annual-report PDF(s)</text>')
    s.append(f'<text x="{CX}" y="{iy+54}" text-anchor="middle" font-size="12.5" fill="#1d4ed8">'
             f'native-text or scanned  ·  one or many reports / years</text>')

    # ---- layer cards ----
    prev_bottom = iy + ih
    for i, L in enumerate(LAYERS):
        cy = card_y(i)
        c = KIND[L["kind"]]

        # arrow + artifact pill from prev node into this card
        _connector(s, prev_bottom, cy, ARTIFACTS[i])
        prev_bottom = cy + CARD_H

        # card + left stripe
        s.append(f'<rect x="{CARD_X}" y="{cy}" width="{CARD_W}" height="{CARD_H}" rx="16" '
                 f'fill="{CARD}" stroke="{CARD_STROKE}" stroke-width="1.5" filter="url(#sh)"/>')
        s.append(f'<rect x="{CARD_X}" y="{cy}" width="9" height="{CARD_H}" rx="4" fill="{c["stripe"]}"/>')

        # header: number badge + name
        ncx = CARD_X + 42
        s.append(f'<circle cx="{ncx}" cy="{cy+40}" r="19" fill="{c["stripe"]}"/>')
        s.append(f'<text x="{ncx}" y="{cy+47}" text-anchor="middle" font-size="19" '
                 f'font-weight="700" fill="#ffffff">{L["n"]}</text>')
        s.append(f'<text x="{CARD_X+74}" y="{cy+37}" font-size="20" font-weight="700" '
                 f'fill="{H1}">{esc(L["name"])}</text>')
        s.append(f'<text x="{CARD_X+74}" y="{cy+62}" font-size="13.5" fill="{BODY}">'
                 f'{esc(L["desc"])}</text>')

        # engine badge (right-aligned)
        bw = int(len(L["badge"]) * 7.2 + 26)
        bx = CARD_X + CARD_W - 20 - bw
        s.append(f'<rect x="{bx}" y="{cy+22}" width="{bw}" height="26" rx="13" fill="{c["bg"]}"/>')
        s.append(f'<text x="{bx+bw//2}" y="{cy+39}" text-anchor="middle" font-size="12.5" '
                 f'font-weight="600" fill="{c["tx"]}">{esc(L["badge"])}</text>')

        # divider
        s.append(f'<line x1="{CARD_X+24}" y1="{cy+78}" x2="{CARD_X+CARD_W-22}" y2="{cy+78}" '
                 f'stroke="{CARD_STROKE}" stroke-width="1.2"/>')

        # three columns: INPUT | INTERNAL | OUTPUT
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

    # ---- output node ----
    ow, oh = 420, 92
    oy = prev_bottom + GAP
    _connector(s, prev_bottom, oy, ARTIFACTS[-1])
    ox = CX - ow // 2
    s.append(f'<rect x="{ox}" y="{oy}" width="{ow}" height="{oh}" rx="14" fill="#f0fdf4" '
             f'stroke="#16a34a" stroke-width="1.5" filter="url(#sh)"/>')
    s.append(f'<text x="{CX}" y="{oy+30}" text-anchor="middle" font-size="17" font-weight="700" '
             f'fill="#14532d">OUTPUT &#8212; Excel workbook (.xlsx)</text>')
    s.append(f'<text x="{CX}" y="{oy+52}" text-anchor="middle" font-size="12.5" fill="#15803d">'
             f'filled template or sheet-per-table  +  Insights / Ledgers / Scope</text>')
    s.append(f'<text x="{CX}" y="{oy+72}" text-anchor="middle" font-size="12" fill="#166534">'
             f'sidecar manifest.json: production-ready flags · sheet→page map · validation</text>')

    # ---- cross-cutting footer ----
    fy = oy + oh + 36
    s.append(f'<text x="{CX}" y="{fy}" text-anchor="middle" font-size="12.5" fill="{MUTE}">'
             f'Cross-cutting&#160;&#160;·&#160;&#160;live progress events&#160;&#160;·'
             f'&#160;&#160;job cancellation&#160;&#160;·&#160;&#160;debug dumps&#160;&#160;·'
             f'&#160;&#160;per-cell source provenance</text>')

    s.append('</svg>')
    out.write_text("\n".join(s), encoding="utf-8")
    print("Wrote", out)


def _connector(s, from_bottom, to_top, label):
    y1 = from_bottom
    y2 = to_top
    s.append(f'<line x1="{CX}" y1="{y1}" x2="{CX}" y2="{y2}" stroke="{ARROW}" '
             f'stroke-width="2" marker-end="url(#arr)"/>')
    # artifact pill centred on the connector
    pw = int(len(label) * 7.0 + 30)
    px = CX - pw // 2
    pcy = (y1 + y2) // 2
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
