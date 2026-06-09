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
    subgroup: str | None = None      # nested block within a group (e.g. "Vision" under Extraction)
    badge: str | None = None         # small tag shown by the UI (e.g. "BETA")


# Section render order; groups not listed fall to the end. "Advanced" is collapsed by default.
_GROUP_ORDER = ("Connection", "Extraction", "Insights", "Validation & Trust",
                "Performance", "Advanced")
_COLLAPSED_GROUPS = {"Advanced"}


# The allowlist. Grouped by USER CONCERN (not subsystem). Declaration order = display order
# within a group; `_GROUP_ORDER` sets section order. `subgroup` nests a block; `advanced`
# fields all live in the (collapsed) "Advanced" group.
_FIELDS: tuple[FieldSpec, ...] = (
    # ── Connection (set up the AI provider) ───────────────────────────────
    FieldSpec("openai_api_key", "Connection", "OpenAI API key",
              "Your OpenAI key. Stored locally; never displayed back.", "secret"),
    FieldSpec("openai_model", "Connection", "OpenAI model",
              "Model id used for extraction and insights (must be vision-capable for vision mode).",
              "str"),
    # ── Extraction (how accurately the document is read) ──────────────────
    FieldSpec("use_gpt_table_extraction", "Extraction", "GPT table extraction",
              "Use GPT to structure financial tables (recommended). Off = rule-based only.", "bool"),
    FieldSpec("ocr_dpi", "Extraction", "OCR resolution (DPI)",
              "Higher = better OCR accuracy but slower and more memory. 200 is a safe default; "
              "vision mode compensates for lower DPI.", "enum", options=(150, 200, 300)),
    FieldSpec("ocr_lang", "Extraction", "OCR language",
              "Tesseract language code(s), e.g. 'eng' or 'eng+fra'.", "str"),
    #   Vision (sub-group of Extraction; sub-knobs reveal when vision is on)
    FieldSpec("use_vision_extraction", "Extraction", "Vision extraction",
              "Send the page image to GPT alongside the text to resolve OCR ambiguity. More "
              "accurate on scanned/complex pages, but uses more tokens.", "bool", subgroup="Vision"),
    FieldSpec("vision_dpi", "Extraction", "Vision render DPI",
              "Resolution of the page image sent to GPT in vision mode.", "int",
              subgroup="Vision", minimum=120, maximum=300, step=10),
    FieldSpec("vision_detail", "Extraction", "Vision detail",
              "OpenAI image detail level.", "enum", subgroup="Vision",
              options=("high", "low", "auto")),
    FieldSpec("vision_max_pages", "Extraction", "Vision max pages",
              "Cap rendered page images per report (0 = all candidate pages).", "int",
              subgroup="Vision", minimum=0, maximum=200, step=1),
    # ── Insights (narrative-generation quality gates) ─────────────────────
    FieldSpec("insight_review_threshold", "Insights", "Review threshold",
              "Insights below this confidence go to the 'Insights Review' sheet.", "float",
              minimum=0.0, maximum=1.0, step=0.05),
    FieldSpec("insight_reject_threshold", "Insights", "Reject threshold",
              "Insights below this confidence are dropped entirely.", "float",
              minimum=0.0, maximum=1.0, step=0.05),
    # ── Validation & Trust (auditability) ─────────────────────────────────
    FieldSpec("validation_review_enabled", "Validation & Trust", "Validation review",
              "Highlights figures worth double-checking. These are suggestions — always confirm "
              "against the actual annual report.", "bool", badge="BETA"),
    # ── Performance (speed / memory / API throughput) ─────────────────────
    FieldSpec("ocr_max_workers", "Performance", "OCR workers",
              "Parallel OCR processes. Higher = faster but more memory; too high can crash "
              "ingest on large scanned PDFs.", "int", minimum=1, maximum=_CPU, step=1),
    FieldSpec("gpt_table_workers", "Performance", "GPT extraction workers",
              "Concurrent GPT calls during table extraction. Raise if your API plan has rate "
              "headroom; lower if you see 429s.", "int", minimum=1, maximum=24, step=1),
    FieldSpec("insights_workers", "Performance", "Insight workers",
              "Concurrent GPT calls for narrative insight extraction.", "int",
              minimum=1, maximum=16, step=1),
    # ── Advanced (rarely touched; can degrade output if mis-set) ──────────
    FieldSpec("llm_max_output_tokens", "Advanced", "Max response length (tokens)",
              "Upper bound on the model's reply length. Keep this generous (≥1500): the "
              "agent's step-by-step tool calls need room, and too low truncates its answer so "
              "you get a raw data dump instead of an explanation.", "int",
              advanced=True, minimum=256, maximum=4000, step=100),
    FieldSpec("llm_json_temperature", "Advanced", "Reasoning temperature",
              "Creativity for the model's structured decisions (intent, agent steps, "
              "verification). 0 = most deterministic and consistent. Note: some models "
              "(e.g. gpt-5-mini) ignore this and use their default.", "float",
              advanced=True, minimum=0.0, maximum=2.0, step=0.1),
    FieldSpec("llm_text_temperature", "Advanced", "Wording temperature",
              "Creativity for phrasing the written answer. Higher = more varied wording; the "
              "facts and numbers are unaffected (guarded). 0 = most consistent.", "float",
              advanced=True, minimum=0.0, maximum=2.0, step=0.1),
    FieldSpec("template_match_threshold", "Advanced", "Template match strictness",
              "Label-similarity needed to map an extracted line to a template row.", "float",
              advanced=True, minimum=0.0, maximum=1.0, step=0.01),
    FieldSpec("metric_fuzzy_threshold", "Advanced", "Metric match strictness",
              "Similarity needed to resolve a line label to a canonical metric.", "float",
              advanced=True, minimum=0.0, maximum=1.0, step=0.01),
    FieldSpec("openai_timeout", "Advanced", "OpenAI timeout (s)",
              "Per-request timeout for GPT calls.", "int", advanced=True,
              minimum=10, maximum=600, step=5),
    FieldSpec("openai_max_retries", "Advanced", "OpenAI max retries",
              "How many times the SDK retries transient errors (429/5xx/timeout).", "int",
              advanced=True, minimum=0, maximum=10, step=1),
    FieldSpec("gpt_table_max_pages", "Advanced", "Max pages sent to GPT",
              "Cap on financial pages sent to GPT per report. Lower = cheaper but may drop "
              "pages on long filings; raise for very long reports.", "int",
              advanced=True, minimum=10, maximum=300, step=10),
    FieldSpec("llm_max_input_chars", "Advanced", "Max prompt input (chars)",
              "Truncate prompt text above this many characters (a cost guard).", "int",
              advanced=True, minimum=4000, maximum=64000, step=1000),
    FieldSpec("metric_use_embeddings", "Advanced", "Semantic metric matching",
              "Use embeddings as a fallback when mapping a label to a canonical metric "
              "(better synonym matching, a little slower).", "bool", advanced=True),
    FieldSpec("tesseract_cmd", "Advanced", "Tesseract path",
              "Path to the Tesseract OCR binary if it isn't on the system PATH. Blank = "
              "auto-detect.", "str", advanced=True),
)

_BY_KEY = {f.key: f for f in _FIELDS}
_SECRET_LOGICAL = {  # spec.key -> logical name used by secrets_status()
    "openai_api_key": "openai",
}


def _invalidate_runtime_caches() -> None:
    """Drop the cached FIE LLM singleton so model/key/temperature changes take effect on the
    NEXT query (the engine is rebuilt per query, only the LLM is cached). Best-effort and
    lazily imported to avoid a route import cycle."""
    try:
        from app.api.routes import fie as _fie
        _fie._llm.cache_clear()
    except Exception:  # noqa: BLE001 — never let cache cleanup break a settings write
        pass


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
            "key": f.key, "group": f.group, "subgroup": f.subgroup, "badge": f.badge,
            "label": f.label, "help": f.help, "kind": f.kind, "advanced": f.advanced,
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
    # Ordered section list so the UI renders groups deterministically (Advanced collapsed).
    present = [f.group for f in _FIELDS]
    ordered = [g for g in _GROUP_ORDER if g in present] + \
              [g for g in present if g not in _GROUP_ORDER]
    seen: set = set()
    groups = [{"name": g, "collapsed": g in _COLLAPSED_GROUPS}
              for g in ordered if not (g in seen or seen.add(g))]
    return {"fields": fields, "groups": groups}


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
    _invalidate_runtime_caches()   # so model/temperature changes apply on the next query
    return _snapshot()


@router.post("/reset")
async def reset_settings():
    """Reset ALL settings to their defaults in one click (clears the override file)."""
    reset_overrides()
    _invalidate_runtime_caches()
    return _snapshot()
