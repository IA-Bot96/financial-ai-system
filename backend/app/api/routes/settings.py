"""Settings API: read/update the user-tweakable engine knobs from the frontend.

Design (see app/core/config.py for the merge precedence):
  - Only an explicit ALLOWLIST of fields is exposed/accepted — never arbitrary attrs,
    never security/ops settings (rate limits, body caps, CORS, paths).
  - Values persist to storage/settings.json and apply to the NEXT extraction run
    (the settings cache is dropped on write; jobs build fresh objects).
  - Secrets (API keys) are WRITE-ONLY: GET returns `configured: true/false`, never the value.
  - `POST /reset` clears every override in one shot (reset-to-defaults).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from app.core.config import (
    Settings,
    get_settings,
    read_overrides,
    reset_overrides,
    secrets_status,
    write_overrides,
)

router = APIRouter(prefix="/settings", tags=["settings"])

_CPU = os.cpu_count() or 4


@dataclass(frozen=True)
class FieldSpec:
    key: str
    group: str
    label: str
    help: str
    kind: str                       # "int" | "float" | "bool" | "enum" | "str" | "secret"
    advanced: bool = False
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple | None = None     # for kind == "enum"


# The allowlist. Order is display order; `group`/`advanced` drive UI sectioning.
_FIELDS: tuple[FieldSpec, ...] = (
    # --- Performance / memory ---
    FieldSpec("ocr_max_workers", "Performance", "OCR workers",
              "Parallel OCR processes. Higher = faster but more memory; too high can crash "
              "ingest on large scanned PDFs.", "int", minimum=1, maximum=_CPU, step=1),
    FieldSpec("ocr_dpi", "Performance", "OCR resolution (DPI)",
              "Higher = better OCR accuracy but slower and more memory. 200 is a safe default; "
              "vision mode compensates for lower DPI.", "enum", options=(150, 200, 300)),
    FieldSpec("gpt_table_workers", "Performance", "GPT extraction workers",
              "Concurrent GPT calls during table extraction. Raise if your API plan has rate "
              "headroom; lower if you see 429s.", "int", minimum=1, maximum=24, step=1),
    # --- Quality ---
    FieldSpec("use_vision_extraction", "Quality", "Vision extraction",
              "Send the page image to GPT alongside the text to resolve OCR ambiguity. More "
              "accurate on scanned/complex pages, but uses more tokens.", "bool"),
    FieldSpec("use_gpt_table_extraction", "Quality", "GPT table extraction",
              "Use GPT to structure financial tables (recommended). Off = rule-based only.", "bool"),
    # --- Credentials ---
    FieldSpec("openai_api_key", "Credentials", "OpenAI API key",
              "Your OpenAI key. Stored locally; never displayed back.", "secret"),
    FieldSpec("openai_model", "Credentials", "OpenAI model",
              "Model id used for extraction and insights (must be vision-capable for vision mode).",
              "str"),
    # --- Vision (advanced) ---
    FieldSpec("vision_dpi", "Vision", "Vision render DPI",
              "Resolution of the page image sent to GPT in vision mode.", "int",
              advanced=True, minimum=120, maximum=300, step=10),
    FieldSpec("vision_detail", "Vision", "Vision detail",
              "OpenAI image detail level.", "enum", advanced=True,
              options=("high", "low", "auto")),
    FieldSpec("vision_max_pages", "Vision", "Vision max pages",
              "Cap rendered page images per report (0 = all candidate pages).", "int",
              advanced=True, minimum=0, maximum=200, step=1),
    # --- Insights (advanced) ---
    FieldSpec("insights_workers", "Insights", "Insight workers",
              "Concurrent GPT calls for narrative insight extraction.", "int",
              advanced=True, minimum=1, maximum=16, step=1),
    FieldSpec("insight_review_threshold", "Insights", "Review threshold",
              "Insights below this confidence go to the 'Insights Review' sheet.", "float",
              advanced=True, minimum=0.0, maximum=1.0, step=0.05),
    FieldSpec("insight_reject_threshold", "Insights", "Reject threshold",
              "Insights below this confidence are dropped entirely.", "float",
              advanced=True, minimum=0.0, maximum=1.0, step=0.05),
    # --- Matching (advanced) ---
    FieldSpec("template_match_threshold", "Matching", "Template match strictness",
              "Label-similarity needed to map an extracted line to a template row.", "float",
              advanced=True, minimum=0.0, maximum=1.0, step=0.01),
    FieldSpec("metric_fuzzy_threshold", "Matching", "Metric match strictness",
              "Similarity needed to resolve a line label to a canonical metric.", "float",
              advanced=True, minimum=0.0, maximum=1.0, step=0.01),
    FieldSpec("ocr_lang", "Matching", "OCR language",
              "Tesseract language code(s), e.g. 'eng' or 'eng+fra'.", "str", advanced=True),
    FieldSpec("openai_timeout", "Matching", "OpenAI timeout (s)",
              "Per-request timeout for GPT calls.", "int", advanced=True,
              minimum=10, maximum=600, step=5),
    FieldSpec("openai_max_retries", "Matching", "OpenAI max retries",
              "How many times the SDK retries transient errors (429/5xx/timeout).", "int",
              advanced=True, minimum=0, maximum=10, step=1),
)

_BY_KEY = {f.key: f for f in _FIELDS}
_SECRET_LOGICAL = {  # spec.key -> logical name used by secrets_status()
    "openai_api_key": "openai",
}


class SettingsUpdate(BaseModel):
    values: dict[str, object]   # {field_key: new_value} for changed fields only


def _coerce_and_validate(spec: FieldSpec, value):
    """Coerce a JSON value to the field's type and enforce range/options. Returns the
    coerced value or raises HTTPException(400)."""
    try:
        if spec.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError("expected true/false")
            return value
        if spec.kind == "int":
            v = int(value)
        elif spec.kind == "float":
            v = float(value)
        elif spec.kind in ("str", "secret"):
            v = str(value)
        elif spec.kind == "enum":
            v = value
            if v not in spec.options:
                raise ValueError(f"must be one of {list(spec.options)}")
            return v
        else:
            raise ValueError(f"unsupported kind {spec.kind}")
    except (TypeError, ValueError) as e:
        raise HTTPException(400, detail=f"{spec.key}: {e}")

    if spec.minimum is not None and v < spec.minimum:
        raise HTTPException(400, detail=f"{spec.key}: must be >= {spec.minimum}")
    if spec.maximum is not None and v > spec.maximum:
        raise HTTPException(400, detail=f"{spec.key}: must be <= {spec.maximum}")
    return v


def _snapshot() -> dict:
    """Current effective values + metadata + defaults, for the settings page to render."""
    s = get_settings()
    overridden = set(read_overrides().keys())
    secrets = secrets_status(s)
    fields = []
    for f in _FIELDS:
        item = {
            "key": f.key, "group": f.group, "label": f.label, "help": f.help,
            "kind": f.kind, "advanced": f.advanced,
            "minimum": f.minimum, "maximum": f.maximum, "step": f.step,
            "options": list(f.options) if f.options else None,
            "overridden": f.key in overridden,
        }
        if f.kind == "secret":
            item["configured"] = secrets.get(_SECRET_LOGICAL.get(f.key, ""), False)
            # never expose secret value or default
        else:
            item["value"] = getattr(s, f.key)
            item["default"] = Settings.model_fields[f.key].default
        fields.append(item)
    return {"fields": fields}


@router.get("")
async def get_settings_page():
    return _snapshot()


@router.post("")
async def update_settings(update: SettingsUpdate):
    if not update.values:
        raise HTTPException(400, detail="No values provided.")
    unknown = [k for k in update.values if k not in _BY_KEY]
    if unknown:
        raise HTTPException(400, detail=f"Unknown setting(s): {unknown}")

    overrides = read_overrides()
    for key, raw in update.values.items():
        spec = _BY_KEY[key]
        coerced = _coerce_and_validate(spec, raw)
        if spec.kind == "secret" and not str(coerced).strip():
            # empty secret = "clear it" -> drop the override so .env/default applies again
            overrides.pop(key, None)
            continue
        overrides[key] = coerced

    # Final guard: the merged overrides must produce a valid Settings (catches anything
    # the per-field check missed) BEFORE we persist — never brick get_settings().
    try:
        Settings(**overrides)
    except ValidationError as e:
        raise HTTPException(400, detail=f"Invalid settings: {e.errors()}")

    write_overrides(overrides)     # persists + clears the settings cache
    return _snapshot()


@router.post("/reset")
async def reset_settings():
    """Reset ALL settings to their defaults in one click (clears the override file)."""
    reset_overrides()
    return _snapshot()
