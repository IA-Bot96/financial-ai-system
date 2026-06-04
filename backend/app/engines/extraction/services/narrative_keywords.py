"""Knowledge base for narrative insight extraction: sections, weights, keywords."""
from __future__ import annotations

# Canonical narrative section -> heading aliases (lowercased).
NARRATIVE_SECTIONS: dict[str, tuple[str, ...]] = {
    "Management Discussion & Analysis": (
        "management discussion", "management s discussion and analysis", "md a",
    ),
    "Business Review": (
        "business review", "review of operations", "operational review",
        "operating review", "performance review",
    ),
    "Directors Report": (
        "directors report", "directors report to the members",
        "report of the directors", "report of the board of directors",
    ),
    "CEO Review": (
        "ceo review", "ceo s review", "ceo s message", "ceo message",
        "message from the ceo", "chief executive s review",
        "chief executive officer s review", "chief executive s message",
        "president s review", "president s message",
    ),
    "Chairman Review": (
        "chairman review", "chairman s review", "chairman s message",
        "chairman s statement", "chairman statement", "message from the chairman",
    ),
    "Outlook": ("outlook", "future outlook", "future prospects"),
    "Financial Review": ("financial review", "financial performance review"),
    "Risks": ("principal risks", "risk management", "risks and opportunities", "risk"),
    "Opportunities": ("opportunities",),
    "Strategy": ("our strategy", "strategy", "strategic"),
    "Sustainability": ("sustainability", "esg", "environmental social"),
}

# Section value weight used by the chunk ranker.
SECTION_WEIGHTS: dict[str, float] = {
    "Management Discussion & Analysis": 5.0,
    "Business Review": 4.5,
    "Directors Report": 4.0,
    "CEO Review": 3.5,
    "Financial Review": 3.0,
    "Chairman Review": 3.0,
    "Outlook": 3.0,
    "Risks": 2.5,
    "Opportunities": 2.5,
    "Strategy": 2.5,
    "Sustainability": 1.5,
}
# Order used for section-balanced round-robin retrieval.
SECTION_ORDER: tuple[str, ...] = tuple(NARRATIVE_SECTIONS.keys())

# High-signal financial/business terms that boost a chunk's score.
FINANCIAL_KEYWORDS: tuple[str, ...] = (
    "expansion", "capacity", "capex", "debt", "borrowings", "exports", "export",
    "cost", "margin", "working capital", "inventory", "receivables", "payables",
    "regulatory", "inflation", "exchange rate", "interest rate", "raw material",
    "energy", "coal", "oil", "freight", "risk", "opportunity", "outlook", "esg",
    "sustainability", "revenue", "sales", "profit", "eps", "dividend",
    "acquisition", "investment", "demand",
)

# Pages containing these are boilerplate / terminal: stop assigning narrative.
TERMINAL_KEYWORDS: tuple[str, ...] = (
    "independent auditor", "auditor's report", "auditors' report",
    "notes to the financial statements", "notes to the unconsolidated",
    "notes to the consolidated", "statement of financial position",
    "statement of profit or loss", "statement of cash flows",
    "statement of changes in equity", "pattern of shareholding",
    "pattern of holding", "notice of annual general meeting", "form of proxy",
    "proxy form", "corporate information", "corporate calendar",
)
