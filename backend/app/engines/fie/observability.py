"""Per-query observability for the engine controller.

Two layers, mirroring the extraction (OCR) pipeline:

  1. ALWAYS-ON structured logs — every layer (PLAN / FETCH / COMPOSE / VERIFY / DONE) emits
     INFO lines carrying its decisive input/output (the plan's needs, each need's fetch outcome,
     the compose verdict + gate results, the final source/citations). These go to the root logger
     (console + whatever the app configures), so the log alone is meant to be enough to root-cause
     a query without re-running it.

  2. DEBUG-GATED deep dump — when ``settings.debug`` is on, each query also gets its own log file
     (``per_query_log``) capturing everything at DEBUG, PLUS full-fidelity per-layer JSON artifacts
     under ``logs/debug/fie/<trace_id>/`` (the exact LLM input payloads, raw plan/compose output,
     every fetched need with its evidence/calcs, the verify decision, citations, the final
     response) AND every LLM prompt/response (``gpt/NNN_<phase>_req.txt`` + ``_resp.json``).

The artifact dumps are no-ops when DEBUG is off, so the controller can call them unconditionally.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager

from app.core.debug import make_fie_dumper
from app.core.logging import per_query_log

_log = logging.getLogger("app.engines.fie")


class _RecordingLLM:
    """Transparent wrapper around the engine's LLM that dumps every call's prompt + response
    (request written first, so even a failing call leaves a record). ``phase`` labels the dump
    file so a reader can tell a PLAN call from a COMPOSE call. Only used when DEBUG dumps are on."""

    def __init__(self, inner, dumper) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_dumper", dumper)
        object.__setattr__(self, "phase", "llm")
        # expose web_search only if the wrapped LLM actually has it (preserves hasattr() guards)
        if hasattr(inner, "web_search"):
            object.__setattr__(self, "web_search", self._web_search)

    def __getattr__(self, name):  # forward .model / ._api_key / .last_error / anything else
        return getattr(self._inner, name)

    def complete_json(self, system, user, schema=None):
        base = self._dumper.gpt_request(self.phase, system, user)
        try:
            resp = self._inner.complete_json(system, user, schema)
        except Exception as exc:  # noqa: BLE001
            self._dumper.gpt_response(base, {"error": repr(exc)})
            raise
        self._dumper.gpt_response(base, resp)
        return resp

    def complete_text(self, system, user):
        base = self._dumper.gpt_request(f"{self.phase}_text", system, user)
        try:
            resp = self._inner.complete_text(system, user)
        except Exception as exc:  # noqa: BLE001
            self._dumper.gpt_response(base, {"error": repr(exc)})
            raise
        self._dumper.gpt_response(base, resp)
        return resp

    def _web_search(self, q):
        base = self._dumper.gpt_request("web_search", "[hosted web_search tool]", q)
        try:
            resp = self._inner.web_search(q)
        except Exception as exc:  # noqa: BLE001
            self._dumper.gpt_response(base, {"error": repr(exc)})
            raise
        self._dumper.gpt_response(base, resp)
        return resp


class RunLog:
    """Handle the controller uses to dump a layer's I/O and set the current LLM phase label.
    Every method is a no-op when DEBUG dumps are disabled, so calls are unconditional."""

    def __init__(self, dumper, llm) -> None:
        self.dumper = dumper
        self._llm = llm

    @property
    def enabled(self) -> bool:
        return self.dumper.enabled

    def dump(self, name: str, obj) -> None:
        if self.dumper.enabled:
            self.dumper.json(name, obj)

    def phase(self, p: str) -> None:
        if isinstance(self._llm, _RecordingLLM):
            self._llm.phase = p


@contextmanager
def run_context(engine, trace_id: str, query: str, audience: str):
    """Set up per-query observability and yield a :class:`RunLog`.

    Always logs a QUERY header (so every answer is traceable in the central log). In DEBUG it
    also: opens a per-query log file, wraps ``engine.llm`` to capture prompts/responses, and
    enables artifact dumps — restoring the original LLM on exit."""
    dumper = make_fie_dumper(trace_id)
    inner = engine.llm
    with ExitStack() as stack:
        if dumper.enabled:
            dumper.subject(trace_id)
            stack.enter_context(per_query_log(trace_id))
            engine.llm = _RecordingLLM(inner, dumper)
            stack.callback(lambda: setattr(engine, "llm", inner))
        _log.info("QUERY trace=%s audience=%s q=%r", trace_id, audience, query,
                  extra={"component": "Engine"})
        try:
            yield RunLog(dumper, engine.llm)
        except Exception:
            _log.exception("query FAILED trace=%s q=%r", trace_id, query,
                           extra={"component": "Engine"})
            raise
