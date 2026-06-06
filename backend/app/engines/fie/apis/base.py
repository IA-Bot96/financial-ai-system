"""API orchestration base (L3b) — Phase 4.

A resilient, spec-driven client: timeout, retry-with-backoff, per-spec circuit
breaker, and last-good cache. Network is injected via a ``Transport`` so the
layer is fully testable offline. Every external datum is normalized to an
``EvidenceItem`` carrying its own source_ref/retrieved_at/reliability so it is
cited and conflict-checked exactly like internal data (architecture §4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, runtime_checkable

from ..models import Citation, EvidenceItem


@runtime_checkable
class Transport(Protocol):
    def get(self, url: str, params: dict, timeout: float) -> dict:
        """Perform a GET and return parsed JSON, or raise on any failure."""

    def post(self, url: str, body: dict, timeout: float, content_type: str = "json"):
        """Perform a POST (json or form-encoded) and return the body, or raise."""


class HttpTransport:
    """Lazy real transport (httpx). Never exercised in tests (no network).

    Returns the body in the form downstream parsers expect, auto-detected from the
    content type: JSON -> dict, HTML -> text, XLSX/binary -> bytes.
    """

    @staticmethod
    def _body(resp):
        ct = resp.headers.get("content-type", "").lower()
        if "json" in ct:
            return resp.json()
        if "html" in ct or "text" in ct:
            return resp.text
        if "spreadsheet" in ct or "excel" in ct or "octet-stream" in ct:
            return resp.content
        # default: try JSON, else text
        try:
            return resp.json()
        except Exception:
            return resp.text

    def get(self, url: str, params: dict, timeout: float):
        import httpx  # imported lazily
        resp = httpx.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return self._body(resp)

    def post(self, url: str, body: dict, timeout: float, content_type: str = "json"):
        import httpx
        if content_type == "form":
            resp = httpx.post(url, data=body, timeout=timeout)  # form-urlencoded
        else:
            resp = httpx.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
        return self._body(resp)


def monthly_windows(anchor_iso: str, n: int = 3, span_days: int = 30) -> list[dict]:
    """n consecutive date windows of ~span_days ending at the anchor (most recent
    first) — e.g. the "last 3 months" pattern for announcements/SECP calls."""
    from datetime import date, timedelta
    end = date.fromisoformat(anchor_iso)
    windows = []
    for _ in range(n):
        start = end - timedelta(days=span_days)
        windows.append({"date_from": start.isoformat(), "date_to": end.isoformat()})
        end = start
    return windows


@dataclass
class ApiSpec:
    id: str
    base_url: str
    path: str                       # may contain {placeholders}
    method: str = "GET"             # "GET" | "POST"
    request_body: Optional[dict] = None  # base POST body (merged with per-call body)
    content_type: str = "json"      # POST encoding: "json" | "form"
    response_type: str = "json"     # "json" | "html" | "xlsx" (documents the parser shape)
    reliability_rating: float = 0.9
    refresh_frequency: str = "intraday"
    failure_mode: str = "degrade"   # "cache" | "degrade" | "omit"
    timeout: float = 5.0
    unit_scale: float = 1.0         # multiply raw values to reach "Rupees in thousand"
    # normalizer(raw_json, params, spec, retrieved_at) -> list[EvidenceItem]
    normalizer: Optional[Callable] = None


@dataclass
class CallResult:
    items: list[EvidenceItem] = field(default_factory=list)
    status: str = "ok"             # "ok" | "cached" | "failed"
    retrieved_at: Optional[str] = None
    note: Optional[str] = None


class ApiClient:
    def __init__(
        self, transport: Transport, *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = None,
        max_retries: int = 2,
        backoff_base: float = 0.1,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 30.0,
    ) -> None:
        self.transport = transport
        self._sleep = sleep
        self._clock = clock
        self._now = now or (lambda: "external")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = breaker_cooldown
        self._breaker: dict[str, dict] = {}
        self._cache: dict[tuple, dict] = {}

    def call(self, spec: ApiSpec, *, body: dict | None = None, **params) -> CallResult:
        key = (spec.id, tuple(sorted(params.items())), tuple(sorted((body or {}).items())))

        if self._breaker_open(spec.id):
            return self._on_failure(spec, key, note="circuit open")

        url = spec.base_url.rstrip("/") + "/" + spec.path.lstrip("/")
        try:
            url = url.format(**params)
        except KeyError:
            pass
        post_body = {**(spec.request_body or {}), **(body or {})}

        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                if spec.method.upper() == "POST":
                    raw = self.transport.post(url, post_body, spec.timeout, spec.content_type)
                else:
                    raw = self.transport.get(url, params, spec.timeout)
                retrieved_at = self._now()
                items = (spec.normalizer(raw, params, spec, retrieved_at)
                         if spec.normalizer else [])
                self._reset_breaker(spec.id)
                self._cache[key] = {"items": items, "retrieved_at": retrieved_at}
                return CallResult(items=items, status="ok", retrieved_at=retrieved_at)
            except Exception as exc:  # noqa: BLE001 — transport-agnostic
                last_exc = exc
                if attempt < self.max_retries:
                    self._sleep(self.backoff_base * (2 ** attempt))
        self._record_failure(spec.id)
        return self._on_failure(spec, key, note=f"transport error: {last_exc}")

    # --- failure handling ---
    def _on_failure(self, spec: ApiSpec, key: tuple, *, note: str) -> CallResult:
        if spec.failure_mode == "cache" and key in self._cache:
            c = self._cache[key]
            return CallResult(items=c["items"], status="cached",
                              retrieved_at=c["retrieved_at"], note=note)
        return CallResult(items=[], status="failed", note=note)

    # --- circuit breaker ---
    def _breaker_open(self, sid: str) -> bool:
        st = self._breaker.get(sid)
        if not st or st["opened_at"] is None:
            return False
        if self._clock() - st["opened_at"] >= self.breaker_cooldown:
            st["opened_at"] = None
            st["failures"] = 0
            return False
        return True

    def _record_failure(self, sid: str) -> None:
        st = self._breaker.setdefault(sid, {"failures": 0, "opened_at": None})
        st["failures"] += 1
        if st["failures"] >= self.breaker_threshold:
            st["opened_at"] = self._clock()

    def _reset_breaker(self, sid: str) -> None:
        self._breaker[sid] = {"failures": 0, "opened_at": None}


def external_evidence(claim: str, value: Optional[float], *, spec: ApiSpec,
                      retrieved_at: str, ref_id: str = "C?",
                      unit: str = "Rupees in thousand", extra_loc: dict | None = None
                      ) -> EvidenceItem:
    """Helper for normalizers: build a cited external EvidenceItem."""
    cite = Citation(
        ref_id=ref_id, kind="external",
        display=f"{spec.id} (retrieved {retrieved_at})",
        locator={"source": spec.id, "endpoint": spec.path,
                 "retrieved_at": retrieved_at, **(extra_loc or {})},
        retrieved_at=retrieved_at,
    )
    return EvidenceItem(
        claim=claim, value=value, unit=unit, kind="external",
        citations=[cite], reliability=spec.reliability_rating,
        freshness=retrieved_at, as_of=retrieved_at,
    )
