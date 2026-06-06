"""DEBUG-mode artifact dumping for observability.

When DEBUG is on, the orchestrator dumps each stage's output to disk so a run
can be diagnosed by reading files instead of re-running:

    logs/debug/<run_id>/<subject>/00_summary.json
                                  01_ingest.json        (text-based pages also in .txt)
                                  02_tables.json
                                  03_interpret.json     (GPT-reconstructed tables)
                                  gpt/001_<schema>_req.txt + _resp.json   (every GPT call)
            <run_id>/<company>/   04_multiyear.json     (merged, leaf-consolidated tables)
                                  05_mapping_plan.json
                                  06_face_truth.json    (decided value + provenance per metric/year)
                                  07_facetruth_decisions.json  (selection / identity-reconcile deltas)
                                  08_validation_ledger.json    (tie-out / withhold / reconcile rows)
                                  00_run_summary.json

Pydantic objects -> .json; raw prompts/text -> .txt. Disabled = no-ops, so the
orchestrator can call it unconditionally.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_SLUG = re.compile(r"[^a-zA-Z0-9]+")


def _slug(value: str) -> str:
    return _SLUG.sub("_", (value or "").strip()).strip("_").lower() or "subject"


def _to_json(obj: Any) -> str:
    if hasattr(obj, "model_dump_json"):
        return obj.model_dump_json(indent=2)
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


class DebugDumper:
    """Writes per-stage artifacts under a run dir. No-op when `root` is None."""

    def __init__(self, root: Path | None, dump_gpt: bool = True) -> None:
        self.root = root
        self.dump_gpt = dump_gpt
        self._subject = ""
        self._gpt_seq = 0
        self._lock = threading.Lock()  # GPT calls may run concurrently

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def subject(self, name: str) -> "DebugDumper":
        """Switch the current subject (a PDF stem or company); resets GPT counter."""
        self._subject = _slug(name)
        self._gpt_seq = 0
        if self.enabled:
            (self.root / self._subject).mkdir(parents=True, exist_ok=True)
        return self

    def _write(self, rel: str, content: str) -> None:
        if not self.enabled:
            return
        path = self.root / self._subject / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:  # never let debug dumping break a run
            logger.warning("Debug dump failed for %s: %s", rel, exc)

    def json(self, name: str, obj: Any) -> None:
        self._write(f"{name}.json", _to_json(obj))

    def text(self, name: str, content: str) -> None:
        self._write(f"{name}.txt", content)

    # --- GPT call capture (request + response) ---

    def gpt_request(self, schema_name: str, system: str, user: str) -> str | None:
        if not self.enabled or not self.dump_gpt:
            return None
        with self._lock:
            self._gpt_seq += 1
            seq = self._gpt_seq
        base = f"gpt/{seq:03d}_{_slug(schema_name)}"
        self._write(f"{base}_req.txt", f"SYSTEM:\n{system}\n\nUSER:\n{user}")
        return base

    def gpt_response(self, base: str | None, response: Any) -> None:
        if base is None:
            return
        self._write(f"{base}_resp.json", _to_json(response))


class GPTRecorder:
    """Transparent wrapper around a GPT client that dumps every call's prompt +
    response (request written first, so even a failing call leaves a record)."""

    def __init__(self, gpt: Any, dumper: DebugDumper) -> None:
        self._gpt = gpt
        self._dumper = dumper

    def complete_structured(self, system: str, user: str, schema, images=None):
        # Record an image reference (count), not the base64 blob, to keep dumps readable.
        note = user if not images else f"{user}\n\n[+{len(images)} page image(s) attached]"
        base = self._dumper.gpt_request(getattr(schema, "__name__", "structured"), system, note)
        try:
            resp = self._gpt.complete_structured(system, user, schema, images=images)
        except Exception as exc:  # noqa: BLE001
            self._dumper.gpt_response(base, {"error": str(exc)})
            raise
        self._dumper.gpt_response(base, resp)
        return resp

    def complete_json(self, system: str, user: str, images=None):
        note = user if not images else f"{user}\n\n[+{len(images)} page image(s) attached]"
        base = self._dumper.gpt_request("json", system, note)
        try:
            resp = self._gpt.complete_json(system, user, images=images)
        except Exception as exc:  # noqa: BLE001
            self._dumper.gpt_response(base, {"error": str(exc)})
            raise
        self._dumper.gpt_response(base, resp)
        return resp


def make_dumper(run_id: str) -> DebugDumper:
    """Build a dumper for a run — enabled only in DEBUG mode."""
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.debug:
        return DebugDumper(None)
    root = Path(settings.debug_dump_dir) / run_id
    logger.info("Debug dumps -> %s", root)
    return DebugDumper(root, dump_gpt=settings.debug_dump_gpt)


def make_fie_dumper(trace_id: str) -> DebugDumper:
    """Dumper for FIE queries, rooted at ``logs/debug/fie/`` (keeps FIE runs from
    colliding with extraction run dirs). The caller sets ``.subject(trace_id)`` so each
    query's artifacts land under ``fie/<trace_id>/``. Enabled only in DEBUG mode."""
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.debug:
        return DebugDumper(None)
    root = Path(settings.debug_dump_dir) / "fie"
    logger.info("FIE debug dumps -> %s/%s", root, trace_id)
    return DebugDumper(root, dump_gpt=settings.debug_dump_gpt)
