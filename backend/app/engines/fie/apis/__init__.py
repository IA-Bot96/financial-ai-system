"""External API orchestration (L3b)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .analysis_reports import AnalysisReports
from .announcements import PSXAnnouncements, SECPNotices
from .base import ApiClient, ApiSpec, CallResult, HttpTransport, Transport, monthly_windows
from .fetch import RegistryFetcher
from .forecast import ForecastRepo
from .macro import Macro
from .news import News
from .overview import CompanyOverview
from .payouts import CompanyPayouts
from .psx import PSX
from .registry import REGISTRY as API_REGISTRY, ApiInfo, shortlist
from .symbols import Symbols


@dataclass
class ExternalSources:
    """Bundle of optional external adapters + a peer-company store registry.

    Any field may be None; the engine degrades gracefully and caps confidence
    when a required external source is unavailable (architecture §3.2, §9.2).
    """

    psx: Optional[PSX] = None
    news: Optional[News] = None
    forecast: Optional[ForecastRepo] = None
    announcements: Optional[PSXAnnouncements] = None
    secp: Optional[SECPNotices] = None
    macro: Optional[Macro] = None
    symbols: Optional[Symbols] = None
    company_overview: Optional[CompanyOverview] = None
    payouts: Optional[CompanyPayouts] = None
    analysis_reports: Optional[AnalysisReports] = None
    registry_fetcher: Optional[RegistryFetcher] = None  # generic fetcher for the 17-API catalog
    as_of: Optional[str] = None  # anchor date (ISO) for date-windowed calls
    peers: dict = field(default_factory=dict)  # {company_name: FinancialFactStore}


__all__ = [
    "ApiClient", "ApiSpec", "CallResult", "HttpTransport", "Transport",
    "monthly_windows", "PSX", "News", "Macro", "Symbols", "CompanyOverview",
    "CompanyPayouts", "AnalysisReports", "ForecastRepo", "PSXAnnouncements",
    "SECPNotices", "ExternalSources", "API_REGISTRY", "ApiInfo", "shortlist",
    "RegistryFetcher",
]
