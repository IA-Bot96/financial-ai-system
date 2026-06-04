"""CLI to exercise Layers 1+2: ingest a PDF then detect/classify its tables.

Usage:
    python -m app.engines.extraction.run_tables path/to/report.pdf [--json]
"""
import argparse
from pathlib import Path

from app.core.logging import configure_logging
from app.engines.extraction.pipeline.ingest import ingest_pdf
from app.engines.extraction.pipeline.tables import detect_tables


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect & classify tables in a PDF.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--json", action="store_true", help="Print the full TableSet as JSON")
    args = parser.parse_args()

    configure_logging(debug=True)
    doc = ingest_pdf(args.pdf)
    table_set = detect_tables(args.pdf, doc)

    if args.json:
        print(table_set.model_dump_json(indent=2))
        return

    print(f"File: {table_set.file_name}  year={table_set.report_year}")
    print(f"Sections: {[(s.statement_type.value, s.start_page, s.end_page) for s in table_set.sections]}")
    print("-" * 70)
    for t in table_set.tables:
        flag = "  [needs GPT review]" if t.needs_review else ""
        print(
            f"{t.table_id} | {t.statement_type.value} "
            f"(score={t.classification_score}, {t.orientation.value}) "
            f"pages={t.source.pages if t.source else []} years={t.years}{flag}"
        )


if __name__ == "__main__":
    main()
