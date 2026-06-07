"""Application configuration."""
import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ root (…/backend). When bundled by PyInstaller (sys.frozen) the source tree
# lives inside the archive, so anchor to the directory holding the launched executable
# instead — that's where the desktop shell places writable, per-install resources.
if getattr(sys, "frozen", False):
    BACKEND_ROOT = Path(sys.executable).resolve().parent
else:
    BACKEND_ROOT = Path(__file__).resolve().parents[2]

# Storage can be redirected to a writable per-user location in a packaged app: the
# desktop shell passes FIE_STORAGE_ROOT (e.g. Electron's userData). Falls back to
# backend/storage for normal dev runs.
STORAGE_ROOT = Path(os.environ.get("FIE_STORAGE_ROOT", BACKEND_ROOT / "storage"))


class Settings(BaseSettings):
    # Anchor .env to the backend root so the key/model load regardless of CWD.
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "AI Financial Intelligence - Extraction Engine"
    debug: bool = False
    log_dir: str = str(BACKEND_ROOT / "logs")  # per-document log files

    # --- Security / hardening (Tier-A/B) ---
    # CORS: comma-separated allowed origins (pin to the real frontend in prod).
    cors_allow_origins: str = "http://localhost:4200"
    force_https: bool = False          # prod (Heroku): True -> HSTS + http->https redirect
    # request input limits
    fie_max_query_chars: int = 256     # max NL query length
    max_request_bytes: int = 2_000_000         # max JSON request body (2 MB; uploads exempt)
    max_upload_bytes: int = 200 * 1024 * 1024  # generic upload cap (fallback)
    max_excel_upload_bytes: int = 200 * 1024 * 1024  # Excel workbook upload cap (200 MB)
    max_pdf_upload_bytes: int = 50 * 1024 * 1024     # per-PDF upload cap (50 MB)
    request_timeout_seconds: int = 60          # per-request wall-clock cap (504 on exceed)
    max_external_calls_per_request: int = 12   # hard cap on adapter fan-out per query
    # rate limiting (token bucket: burst `capacity`, refill 1 per `refill_seconds`)
    # default sustained rate = 1 req/sec per IP, with a small burst of 3.
    rate_limit_enabled: bool = True
    rate_limit_capacity: int = 3
    rate_limit_refill_seconds: float = 1.0
    # shared rate-limit/quota backend for multi-instance deploys (blank = in-process)
    redis_url: str = ""
    # LLM cost guard
    llm_max_input_chars: int = 24_000          # truncate prompt input above this
    llm_max_output_tokens: int = 800           # cap completion length
    # per-provider news daily call ceiling (0 = unlimited)
    news_daily_call_cap_per_provider: int = 200
    # persist a per-query reasoning TraceRecord (audit trail) on the live FIE route
    fie_trace_enabled: bool = True
    fie_trace_dir: str = str(BACKEND_ROOT / "logs" / "traces")

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]
    # DEBUG-only: dump each stage's output (+ every GPT prompt/response) to disk.
    debug_dump_dir: str = str(BACKEND_ROOT / "logs" / "debug")
    debug_dump_gpt: bool = True

    # --- Interpretation stage: GPT (read from .env) ---
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    openai_timeout: int = 120
    # The SDK retries transient errors (429/5xx/timeouts) with exponential
    # backoff internally; this just caps the attempts. One shared knob is enough
    # for our two call sites (classification + insights) — no per-call backoff var.
    openai_max_retries: int = 3

    # --- Interpretation stage: insights (sliding-window extraction) ---
    insights_chunk_max_chars: int = 2800
    insights_chunk_overlap: int = 250
    insights_chunks_per_call: int = 8
    insights_max_chunks: int | None = None   # None = cover all ranked chunks
    insights_workers: int = 8                # concurrent insight-batch GPT calls (1 = sequential)
    insight_reject_threshold: float = 0.50   # below -> dropped
    insight_review_threshold: float = 0.70   # below -> 'Insights Review' sheet
    insight_dedup_similarity: float = 0.90   # cosine >= this -> duplicate

    # --- GPT-assisted table extraction (robust on scanned/complex pages) ---
    use_gpt_table_extraction: bool = True
    gpt_table_max_pages: int = 130          # safety cap on pages/report; >= the ~90
                                            # financial pages of a scanned report so the
                                            # cap doesn't silently drop statement pages
    gpt_table_min_money: int = 6            # page needs >= this many comma-grouped figures
    gpt_table_dense_digits: int = 120       # OCR fallback: very digit-dense page (commas lost)
    gpt_table_workers: int = 16             # concurrent page-extraction GPT calls (1 = sequential).
                                            # Raised 8->16: production logs show a ~60-page report
                                            # runs 8 workers for ~106s with ZERO 429s/retries, i.e.
                                            # the account rate limit has headroom and 8 was the
                                            # binding cap. 16 ~halves table-extraction wall-time;
                                            # excess is throttled gracefully by openai_max_retries.
                                            # Watch logs for 429s if raising further.

    # --- Vision-assisted reconstruction (send the page image with the text) ---
    # When on, each financial page sent to GPT for reconstruction also gets a rendered
    # image of that page; GPT treats the image as authoritative to resolve OCR ambiguity
    # (digits, signs, column alignment, merged cells, consolidated/unconsolidated header)
    # while the text supplies exact figures. Requires a vision-capable model.
    use_vision_extraction: bool = False
    vision_dpi: int = 180                    # render DPI for the page image (legibility vs tokens)
    vision_detail: str = "high"             # OpenAI image detail: "high" | "low" | "auto"
    vision_max_pages: int = 0               # cap images/report (0 = all candidate pages)

    # --- Metric resolution (line-item label -> canonical metric) ---
    metric_registry_path: str = ""           # empty -> packaged registry file
    metric_fuzzy_threshold: float = 0.90     # rapidfuzz ratio (0-1) to accept
    metric_use_embeddings: bool = False      # embedding fallback for synonyms

    # --- Layer 5: template mapping ---
    template_match_threshold: float = 0.82   # label similarity (0-1) to accept a row
    template_min_empty_fraction: float = 0.20  # below -> sheet is computed/output, skip

    # --- News providers (free-tier API keys; failover in finance-native order) ---
    # The news layer tries providers in this order and uses the first that returns
    # query-relevant articles; an empty key skips that provider. See
    # app/engines/fie/apis/news_providers.py for the ordering rationale.
    news_marketaux_key: str = ""        # marketaux.com   (finance-native)
    news_finnhub_key: str = ""          # finnhub.io      (finance-native, ticker-only)
    news_alphavantage_key: str = ""     # alphavantage.co (finance-native, ticker-only)
    news_newsdata_io_key: str = ""      # newsdata.io
    news_newsapi_ai_key: str = ""       # newsapi.ai (Event Registry)
    news_worldnewsapi_key: str = ""     # worldnewsapi.com
    news_gnews_io_key: str = ""         # gnews.io
    news_newsapi_org_key: str = ""      # newsapi.org (24h delay, non-commercial free tier)
    news_max_articles: int = 10         # cap articles returned per search

    # --- News semantic retrieval (chunk -> embed -> rank vs query -> dedup) ---
    news_chunk_chars: int = 500         # sliding-window size (~chars) for long bodies
    news_chunk_overlap: int = 100       # window overlap
    news_min_body_chars: int = 600      # below this, the snippet/body is one chunk (no windowing)
    news_similarity_floor: float = 0.20  # min cosine(query, chunk) to keep a chunk
    news_top_k: int = 8                 # max chunks fed to the LLM / surfaced in the response
    news_dedup_similarity: float = 0.92  # cosine >= this -> near-duplicate chunk (keep best)
    news_same_story_similarity: float = 0.85  # cosine >= this -> same story across outlets
                                              # (syndication) -> fold to one independent source
    news_recency_weight: float = 0.2    # blended score = (1-w)*cosine + w*recency
    news_recency_halflife_days: int = 14  # recency half-life for the decay term

    # --- Storage ---
    inputs_dir: Path = STORAGE_ROOT / "inputs"

    # --- OCR settings (rule-based ingest) ---
    # Path to the tesseract binary if it is not on PATH (Windows example:
    # C:\Program Files\Tesseract-OCR\tesseract.exe). Leave blank to use PATH.
    tesseract_cmd: str = ""
    # Optional language(s) for tesseract, e.g. "eng" or "eng+fra".
    ocr_lang: str = "eng"
    # DPI used when rasterizing a page for OCR. Higher = better accuracy, slower.
    ocr_dpi: int = 300
    # A page with fewer than this many extractable characters is treated as
    # scanned/image-only and sent to OCR.
    min_text_chars: int = 40
    # Max worker processes for OCR. Scanned-page OCR (render + tesseract) is the
    # ingest bottleneck; a flat process pool drains all PDFs' scanned pages at once
    # (page- AND document-level parallelism under one core budget). 0 = auto
    # (cpu_count - 1); 1 = serial (the original in-process path).
    ocr_max_workers: int = 0

    # --- Layer 2: table & section detection / classification ---
    # Local, free embedding model (sentence-transformers). No API cost.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    use_embeddings: bool = True
    classify_fuzzy_weight: float = 0.5
    classify_embed_weight: float = 0.5
    classify_accept_threshold: float = 0.60  # combined score needed to accept
    classify_margin: float = 0.08            # gap over the runner-up needed to accept
    classify_section_hint_boost: float = 0.10
    # Word-clustering tolerances (pixels at OCR DPI / PDF points) for table grids.
    table_row_tol: float = 8.0
    table_col_tol: float = 25.0
    # Minimum share of cells containing digits for a word-grid to count as a table.
    table_min_numeric_ratio: float = 0.15

    def ensure_dirs(self) -> None:
        self.inputs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


# secret-bearing settings, by logical name -> attribute (values are NEVER exposed)
_SECRET_ATTRS = {
    "openai": "openai_api_key",
    "news_marketaux": "news_marketaux_key",
    "news_finnhub": "news_finnhub_key",
    "news_alphavantage": "news_alphavantage_key",
    "news_newsdata_io": "news_newsdata_io_key",
    "news_newsapi_ai": "news_newsapi_ai_key",
    "news_worldnewsapi": "news_worldnewsapi_key",
    "news_gnews_io": "news_gnews_io_key",
    "news_newsapi_org": "news_newsapi_org_key",
}


def secrets_status(settings: Settings | None = None) -> dict[str, bool]:
    """Which secrets are configured (presence only — never the value). For startup
    visibility and /readiness, so ops can see a missing key without leaking any."""
    s = settings or get_settings()
    return {name: bool((getattr(s, attr, "") or "").strip())
            for name, attr in _SECRET_ATTRS.items()}
