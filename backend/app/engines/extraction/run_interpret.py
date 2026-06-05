"""CLI: ingest + detect tables + interpret one PDF (requires OPENAI_API_KEY in backend/.env).

Usage:
    python -m app.engines.extraction.run_interpret path/to/report.pdf [--json]
"""
import argparse
from pathlib import Path

from app.core.logging import configure_logging
from app.engines.extraction.pipeline.ingest import ingest_pdf
from app.engines.extraction.pipeline.interpret import interpret_document
from app.engines.extraction.pipeline.tables import detect_tables
from app.engines.extraction.services.gpt_client import GPTClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest, detect tables, and interpret one PDF.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    configure_logging(debug=True)
    doc = ingest_pdf(args.pdf)
    table_set = detect_tables(args.pdf, doc)
    result = interpret_document(doc, table_set, GPTClient())

    if args.json:
        print(result.model_dump_json(indent=2))
        return

    print(f"File: {result.file_name}  year={result.report_year}")
    print(f"Financial tables: {len(result.tables)}")
    for t in result.tables:
        print(f"  - {t.statement_type.value}: {len(t.line_items)} line items, years={t.years}")
    print(f"Insights: {len(result.insights)}")
    for i in result.insights[:10]:
        print(f"  - [{i.area}] {i.takeaway[:80]} (p{i.page}, conf={i.confidence})")


if __name__ == "__main__":
    main()
