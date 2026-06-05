"""End-to-end CLI: PDFs (+ optional template) -> Excel workbook.

Examples:
    # No template -> sheet-per-table workbook
    python -m app.engines.extraction.run_extract --out out.xlsx report-2025.pdf

    # With template -> filled template
    python -m app.engines.extraction.run_extract --out filled.xlsx \
        --template millat-template.xlsx --company "Millat" \
        millat-2022.pdf millat-2023.pdf millat-2024.pdf millat-2025.pdf
"""
import argparse
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.engines.extraction.pipeline.orchestrator import process_reports

logger = get_logger("run")


def main() -> None:
    p = argparse.ArgumentParser(description="Run the full extraction pipeline.")
    p.add_argument("pdfs", nargs="+", type=Path, help="Annual report PDF(s)")
    p.add_argument("--out", required=True, type=Path, help="Output .xlsx path")
    p.add_argument("--template", type=Path, default=None, help="Optional template .xlsx")
    p.add_argument("--company", default=None)
    args = p.parse_args()

    configure_logging(debug=get_settings().debug)
    result = process_reports(
        args.pdfs, args.out, template_path=args.template, company=args.company,
    )

    print("-" * 60)
    print(f"Mode:        {result.mode}")
    print(f"Output:      {result.output_path}")
    print(f"Years:       {result.company.fiscal_years}")
    print(f"Tables:      {len(result.company.tables)}")
    print(f"Insights:    {len(result.company.insights)} (+{len(result.company.insights_review)} review)")
    if result.plan:
        print(f"Cells filled: {len(result.plan.writes)}; sheets: {result.plan.sheets_processed}")
        if result.plan.unmatched_template_labels:
            print(f"Unmatched template labels: {len(result.plan.unmatched_template_labels)}")
    print(f"Production-ready: {result.production_ready}  "
          f"(validation failures: {result.validation_failures}, withheld: {result.withheld}, "
          f"quarantined: {result.quarantined})")
    if not result.production_ready:
        print("  -> NON-PRODUCTION: see the 'Validation Ledger' sheet for the failing rows.")
    if result.manifest_path:
        print(f"Manifest: {result.manifest_path}")


if __name__ == "__main__":
    main()
