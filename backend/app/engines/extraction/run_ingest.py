"""Small CLI to exercise the ingest layer.

Usage:
    python -m app.engines.extraction.run_ingest path/to/report.pdf [--json]
"""
import argparse
import json
from pathlib import Path

from app.core.logging import configure_logging
from app.engines.extraction.pipeline.ingest import ingest_pdf


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a PDF (native text + OCR fallback).")
    parser.add_argument("pdf", type=Path, help="Path to the PDF file")
    parser.add_argument("--json", action="store_true", help="Print the full IngestedDoc as JSON")
    args = parser.parse_args()

    configure_logging(debug=True)
    doc = ingest_pdf(args.pdf)

    if args.json:
        print(doc.model_dump_json(indent=2))
        return

    print(f"File:        {doc.file_name}")
    print(f"Pages:       {doc.page_count} ({doc.ocr_page_count} via OCR)")
    print(f"Report year: {doc.report_year}")
    print(f"Scanned:     {doc.is_scanned}")
    print("-" * 60)
    for p in doc.pages[:3]:
        preview = p.text.strip().replace("\n", " ")[:200]
        print(f"[p{p.page} {p.kind.value} chars={p.char_count} conf={p.ocr_confidence}] {preview}")


if __name__ == "__main__":
    main()
