"""Application configuration."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ root (…/backend)
BACKEND_ROOT = Path(__file__).resolve().parents[2]
STORAGE_ROOT = BACKEND_ROOT / "storage"


class Settings(BaseSettings):
    # Anchor .env to the backend root so the key/model load regardless of CWD.
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "AI Financial Intelligence - Extraction Engine"
    debug: bool = False
    log_dir: str = str(BACKEND_ROOT / "logs")  # per-document log files

    # --- Layer 3: GPT (read from .env) ---
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    openai_timeout: int = 120
    # The SDK retries transient errors (429/5xx/timeouts) with exponential
    # backoff internally; this just caps the attempts. One shared knob is enough
    # for our two call sites (classification + insights) — no per-call backoff var.
    openai_max_retries: int = 3

    # --- Layer 3: insights (sliding-window extraction) ---
    insights_chunk_max_chars: int = 2800
    insights_chunk_overlap: int = 250
    insights_chunks_per_call: int = 8
    insights_max_chunks: int | None = None   # None = cover all ranked chunks
    insight_reject_threshold: float = 0.50   # below -> dropped
    insight_review_threshold: float = 0.70   # below -> 'Insights Review' sheet
    insight_dedup_similarity: float = 0.90   # cosine >= this -> duplicate

    # --- Metric resolution (line-item label -> canonical metric) ---
    metric_registry_path: str = ""           # empty -> packaged registry file
    metric_fuzzy_threshold: float = 0.90     # rapidfuzz ratio (0-1) to accept
    metric_use_embeddings: bool = False      # embedding fallback for synonyms

    # --- Layer 5: template mapping ---
    template_match_threshold: float = 0.82   # label similarity (0-1) to accept a row
    template_min_empty_fraction: float = 0.20  # below -> sheet is computed/output, skip

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
