"""Golden fixtures derived from ocr_millat_output_audit.md (audited PDF face truths).

SCOPE (architecture §0.3): these belong to the development-time validation harness
(Phase 2.8), NOT the runtime answer path. At answer time the workbook figures are
trusted. Authored here in Phase 0; assertions run by the Phase 2.8 validator.
"""

# Rupees in thousand. Source: millat-2025.pdf face statements.
FACE_TRUTH_2025 = {
    "revenue": 52_108_997,
    "cost_of_sales": -38_241_906,
    "gross_profit": 13_867_091,
    "operating_profit": 10_236_479,
    "finance_cost": -2_172_644,
    "pat": 6_372_928,
    "total_equity": 8_076_300,
    "non_current_assets": 8_014_208,
    "current_assets": 24_974_383,
    "total_assets": 32_988_591,
    "total_equity_and_liabilities": 32_988_591,
}

# FY2024 as restated in the FY2025 report.
FACE_TRUTH_2024_RESTATED = {
    "revenue": 91_534_501,
    "pat": 10_224_875,
    "total_assets": 32_873_428,
}

# Known-bad v5 values that the validator must FLAG as MISMATCH (not pass silently).
V5_BAD_VALUES = [
    {"metric": "revenue", "year": 2025, "bad": 57_840_150, "truth": 52_108_997},
    {"metric": "revenue", "year": 2024, "bad": 57_222_177, "truth": 91_534_501},
    {"metric": "pat", "year": 2025, "bad": 12_165_035, "truth": 6_372_928},
    {"metric": "pat", "year": 2024, "bad": -21_956_716, "truth": 10_224_875},
    {"metric": "total_assets", "year": 2025, "bad": 28_214_081, "truth": 32_988_591},
]

# Derived-metric tie-outs the calc engine must reproduce (Phase 2).
DERIVED_ASSERTIONS = {
    "gross_profit_2025": 52_108_997 - 38_241_906,  # == 13_867_091
    "balance_sheet_identity_2025": (8_014_208 + 24_974_383, 32_988_591),
}
