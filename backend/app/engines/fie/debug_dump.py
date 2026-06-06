"""FIE DEBUG-mode artifact dumping helpers.

A thin recorder that wraps the FIE ``LLMClient`` (``complete_json`` / ``complete_text``
— signatures differ from the extraction engine's GPT client, so its GPTRecorder can't be
reused) and captures every prompt + response via the shared ``DebugDumper``. The request
is written before the call, so even a failing call leaves a record; the inner result is
returned verbatim (and exceptions re-raised) so wrapping never changes behavior.
"""

from __future__ import annotations

from typing import Optional

from app.core.debug import DebugDumper


class FieLLMRecorder:
    """LLMClient-compatible wrapper that dumps each call under ``gpt/`` of the dumper."""

    def __init__(self, llm, dumper: DebugDumper) -> None:
        self._llm = llm
        self._d = dumper

    def complete_json(self, system: str, user: str, schema: dict) -> Optional[dict]:
        base = self._d.gpt_request("json", system, user)
        try:
            resp = self._llm.complete_json(system, user, schema)
        except Exception as exc:  # noqa: BLE001 — record then re-raise
            self._d.gpt_response(base, {"error": str(exc)})
            raise
        self._d.gpt_response(base, resp)
        return resp

    def complete_text(self, system: str, user: str) -> Optional[str]:
        base = self._d.gpt_request("text", system, user)
        try:
            resp = self._llm.complete_text(system, user)
        except Exception as exc:  # noqa: BLE001
            self._d.gpt_response(base, {"error": str(exc)})
            raise
        if base is not None:                       # text response -> .txt for readability
            self._d.text(f"{base}_resp", resp if resp is not None else "(None)")
        return resp


def wrap_llms(engine, dumper: DebugDumper) -> list[tuple[object, object]]:
    """Swap every LLM holder on the engine for a recorder sharing ``dumper`` (one GPT
    sequence across the whole query). Returns [(holder, original_llm), …] for restore."""
    holders = [engine, engine.synthesizer, engine.conflicts, engine.insights]
    saved: list[tuple[object, object]] = []
    for h in holders:
        orig = getattr(h, "llm", None)
        if orig is not None:
            saved.append((h, orig))
            h.llm = FieLLMRecorder(orig, dumper)
    return saved


def restore_llms(saved: list[tuple[object, object]]) -> None:
    for holder, orig in saved:
        holder.llm = orig
