# FIE Debug Logging & Per-Layer Artifact Dumps — Requirements & Plan

Status: **implemented** (2026-06-07). Decisions confirmed: per-query `.log` **and** JSON
dumps; gated on the existing `debug` flag (no separate toggle).

### Reading a debug run
Set `DEBUG=true` (env / `.env`) and send a query. Artifacts land under:

```
logs/debug/fie/<trace_id>/
  00_summary.json          run metadata + elapsed_ms
  01_frame.json   02_plan.json
  03_evidence.json 04_calcs.json 05_conflicts.json 06_extra.json
  07_evidence_admitted.json 08_conflicts_final.json
  09_confidence.json 10_reasoning_graph.json 11_llm_analysis.txt
  12_citations.json 13_response.json
  gpt/NNN_<json|text>_req.txt + _resp.(json|txt)   # one pair per LLM call
logs/<timestamp>_<trace_id>.log                      # the per-query log
```
Off by default → nothing written, byte-identical answers. Dumps are unredacted
(financial + article text): **never enable `debug` in production.**

---

#### Original proposal follows.

Scope: bring the FIE engine to parity with the extraction/OCR engine's DEBUG-mode
observability — detailed per-layer logging plus on-disk dumps of every layer's output
(`.json` for structured data, `.txt` for prose), so a query can be diagnosed by reading
files instead of re-running.

---

## 1. Goal

In **debug mode only**, every FIE query writes an ordered set of artifacts to disk — one
per pipeline layer — using the *same mechanism the extraction engine already uses*
(`app/core/debug.DebugDumper`). Structured outputs (pydantic models / dicts) dump as
`.json`; prose (LLM narration) dumps as `.txt`. Every LLM call's prompt + response is
captured. When debug is off, everything is a no-op (zero overhead, zero behavior change).

This is **diagnostic/observability tooling**, not a runtime feature: it must never change
an answer, never raise into the request path, and stay off in production.

---

## 2. Current state (the gap)

### Extraction engine (the reference) — already complete
- `app/core/debug.py`: `DebugDumper` with `.subject(name)`, `.json(name, obj)`,
  `.text(name, content)`, `.gpt_request()/.gpt_response()`; `GPTRecorder` wrapper;
  `make_dumper(run_id)` gated on `settings.debug`.
- `app/core/logging.py`: `EngineFormatter` (derives a `component` column from the module
  name), `per_document_log(document_id)` context manager → one `.log` file per document.
- Orchestrator dumps `01_ingest.json` … `08_validation_ledger.json` + `gpt/NNN_*`.
- Config: `debug`, `debug_dump_dir`, `debug_dump_gpt` (already exist).

### FIE engine — what exists today
- `fie.py::_layer(component, msg, *args)` logs **three coarse summary lines** only
  (`"Understand"`, `"Retrieve"`, `"Respond"` at ~lines 86, 173, 196). No per-layer detail,
  no disk dumps.
- `trace.py::TraceRecord` + `TraceStore.persist()` serialize a **partial** chain
  (`frame`, `plan`, `evidence`, `response`) to one JSON — only on `answer_with_trace()`,
  and missing `calcs`, `conflicts`, `confidence`, the reasoning graph, `ctx.extra`, and LLM
  prompts.
- All major layer outputs are pydantic models (JSON-ready via `model_dump_json`); the only
  free-text output is the LLM narration (`ctx.llm_analysis`) and `Response.supporting_analysis`.

**Conclusion:** reuse `DebugDumper` verbatim; add a FIE-specific LLM recorder (signature
differs — see §6); insert dump calls at the seams in `_run`; enrich the per-layer logs.

---

## 3. Layer → artifact map

Execution order in `fie.py::_run`, with the variable dumped, its type, and the format.
File names are numeric-prefixed so a directory listing reads in pipeline order.

| # | Layer | Source var (in `_run`) | Type | Artifact | Fmt |
|---|-------|------------------------|------|----------|-----|
| 00 | Run summary | trace_id, query, audience, intent, company, year, degraded, partial_coverage, timings | dict | `00_summary.json` | json |
| 01 | L1 Understanding | `frame` | `QueryFrame` | `01_frame.json` | json |
| 02 | L2 Planning | `plan` | `SourcePlan` | `02_plan.json` | json |
| 03 | L3 Retrieval/intent evidence | `ctx.evidence` (post intent branch) | `list[EvidenceItem]` | `03_evidence.json` | json |
| 04 | L4 Calculations | `ctx.calcs` | `list[CalcResult]` | `04_calcs.json` | json |
| 05 | L6a Conflicts (intra) | `ctx.conflicts` (post intent branch) | `list[Conflict]` | `05_conflicts.json` | json |
| 06 | Intent extras | `ctx.extra` (+ `selected_insights`, `insight_resolutions` for risk) | dict | `06_extra.json` | json |
| 07 | L6b Admission + corroboration | `ctx.evidence` (roles stamped, external corroboration merged) | `list[EvidenceItem]` | `07_evidence_admitted.json` | json |
| 08 | L6a Conflicts (cross-source) | `ctx.conflicts` (after internal-vs-external + cross-api) | `list[Conflict]` | `08_conflicts_final.json` | json |
| 09 | L7 Confidence | `conf` | `ConfidenceReport` | `09_confidence.json` | json |
| 10 | L5 Reasoning graph | `graph` | `ReasoningGraph` | `10_reasoning_graph.json` | json |
| 11 | L5 Narration | `ctx.llm_analysis` | `str` / None | `11_llm_analysis.txt` | text |
| 12 | L8a Citations | `cites`, `withheld` | `list[Citation]`, `list[FactRef]` | `12_citations.json` | json |
| 13 | L8b Response | `resp` | `Response` | `13_response.json` | json |
| — | LLM calls | every `complete_json`/`complete_text` | prompt+resp | `gpt/NNN_<kind>_req.txt` / `_resp.json` | txt+json |

Output dir: `logs/debug/fie/<trace_id>/…` (the `fie/` segment keeps FIE runs from
colliding with extraction `run_id` dirs under the same `debug_dump_dir`).

---

## 4. Requirements

**Functional**
1. When `settings.debug` is true, each query writes artifacts 00–13 (those that apply to
   the intent) under `logs/debug/fie/<trace_id>/`.
2. Format rule (inherited from `DebugDumper`): pydantic/dict → `.json` via
   `model_dump_json(indent=2)`; prose → `.txt`. No new format logic.
3. Every LLM prompt+response is captured under `gpt/` (request written before the call so a
   failing call still leaves a record), gated additionally by `debug_dump_gpt`.
4. Per-query detail logs: richer DEBUG-level lines per layer (counts + key identifiers),
   keeping the existing INFO summary lines intact for backward compatibility.
5. Optionally, a per-query `.log` file (one file per `trace_id`) via the existing
   `per_document_log` mechanism, so a single query's full log is isolated.

**Non-functional**
6. **No-op when debug is off** — identical behavior and ~zero overhead (the dumper short-
   circuits on `enabled`).
7. **Never breaks a request** — dump failures are swallowed (already true in `DebugDumper._write`);
   the recorder must not alter LLM results or swallow real errors silently (it re-raises).
8. **No new behavior in the answer path** — dumps are write-only observations of existing vars.
9. **Test isolation** — the suite runs with debug off; new tests force debug on against a
   `tmp_path`, so the default 404-test suite is unaffected.

---

## 5. Design decisions (please confirm)

- **D1 — Reuse `DebugDumper` as-is.** No changes to `app/core/debug.py` except possibly a
  thin `make_fie_dumper(trace_id)` helper (or just call `make_dumper(f"fie/{trace_id}")`).
  *Recommended: add `make_fie_dumper` for clarity.*
- **D2 — `subject` = `trace_id`.** Mint the id once at the top of `_run` via the existing
  `self._trace_id()` factory (today it's only used in `answer_with_trace`). This means
  **every** `answer()` call gets an id (cheap; also improves traceability).
- **D3 — FIE-specific LLM recorder** (`FieLLMRecorder`) implementing the `LLMClient`
  protocol (`complete_json(system, user, schema)`, `complete_text(system, user)`) — the
  extraction `GPTRecorder` can't be reused because its signatures differ. Wrap `self.llm`
  for the duration of `_run` only when debug is on.
- **D4 — Dump location** `logs/debug/fie/<trace_id>/`. Reuse `debug_dump_dir`; no new dir
  config required. Add one optional flag `debug_dump_fie: bool = True` so FIE dumps can be
  toggled independently of extraction dumps (both still require `debug`).
- **D5 — Keep `_layer` INFO lines**; add `_log.debug(...)` detail lines. Detail logs only
  materialize at DEBUG level, so production INFO logs are unchanged.
- **D6 — Sensitive content**: dumps contain financial figures and external article text
  (and LLM prompts, which may include that text). This is acceptable for **local debug
  only** — documented as "do not enable `debug` in production; dumps are unredacted." The
  log *stream* stays redacted via the existing `RedactingFilter`; file dumps are not
  redacted by design (they're the diagnostic artifact).

---

## 6. Step-by-step implementation plan

> Each step is independently testable. Estimated 1 focused pass.

**Step 1 — Config (app/core/config.py)**
Add `debug_dump_fie: bool = True` (only active when `debug` is also true). Reuse existing
`debug`, `debug_dump_dir`, `debug_dump_gpt`.

**Step 2 — Dumper helper (app/core/debug.py)**
Add `make_fie_dumper(trace_id: str) -> DebugDumper`: returns `DebugDumper(None)` unless
`settings.debug and settings.debug_dump_fie`, else `DebugDumper(debug_dump_dir / "fie" / trace_id)`.
(Thin; no change to the class.)

**Step 3 — FIE LLM recorder (app/engines/fie/llm.py or a new `fie/_debug.py`)**
Add `FieLLMRecorder(llm, dumper)` implementing `LLMClient`:
- `complete_json(system, user, schema)` → `base = dumper.gpt_request("json", system, user)`;
  call inner; `dumper.gpt_response(base, result)`; return result (re-raise on error after
  recording).
- `complete_text(system, user)` → same with `schema_name="text"`; response written as text
  via a small shim (wrap the string so `_to_json` stays valid, or add a `text` response path).
- Must be a structural `LLMClient` (the engine type-checks via Protocol).

**Step 4 — Wire into the orchestrator (app/engines/fie/fie.py::_run)**
1. Top of `_run`: `trace_id = self._trace_id()`; `dumper = make_fie_dumper(trace_id)`;
   `dumper.subject(trace_id)`; if `dumper.enabled`, temporarily swap `self.llm` /
   `self.synthesizer.llm` for `FieLLMRecorder(...)` (restore in a `finally`).
2. After `understand`/`plan`: `dumper.json("01_frame", frame)`, `dumper.json("02_plan", plan)`.
3. After the intent branch: dump `03_evidence`, `04_calcs`, `05_conflicts`, `06_extra`
   (+ insights for risk).
4. After role-stamping + corroboration + cross-source detectors: `07_evidence_admitted`,
   `08_conflicts_final`.
5. After confidence: `09_confidence`. After `build_graph`/`narrate`: `10_reasoning_graph`,
   `11_llm_analysis` (text). After `bind`: `12_citations`. After `render`: `13_response`.
6. Write `00_summary` last (intent, company, year, flags, per-layer timings).
7. Add `_log.debug(...)` detail lines alongside each existing `_layer(...)` INFO line.

**Step 5 — Per-query log file (optional, app/core/logging.py)**
Add `per_query_log(trace_id)` (thin alias over `per_document_log`) or reuse
`per_document_log(trace_id)` around `_run` so each query's logs land in
`logs/<ts>_<trace_id>.log`. Decision: include only if we want isolated per-query log files
in addition to the dumps.

**Step 6 — Timings**
Wrap each layer call in a lightweight monotonic timer (only when `dumper.enabled`) to fill
`00_summary.timings`. Avoid `time` in hot path when disabled.

**Step 7 — Tests (tests/fie/test_debug_dump.py)**
- debug off → `answer()` writes nothing (no `logs/debug/fie/...`).
- debug on (monkeypatch settings + `tmp_path` as `debug_dump_dir`) → after a ratio query,
  assert `01_frame.json`, `04_calcs.json`, `13_response.json` exist and parse; `frame.json`
  round-trips to a `QueryFrame`.
- with a fake LLM → `gpt/001_*_req.txt` + `_resp.json` written; recorder returns the inner
  result unchanged and re-raises on inner error after recording.
- `FieLLMRecorder` satisfies `isinstance(rec, LLMClient)`.

**Step 8 — Docs**
Update this file's status to "implemented"; add a short "Reading a debug run" section
(dir layout + what each file means) and a `DEBUG=true` usage note in `.env.example`.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Dumping changes behavior | Write-only; vars already exist; recorder returns inner result verbatim. |
| Overhead when off | `DebugDumper(None)` no-ops; timers only when enabled; LLM unwrapped when off. |
| LLM recorder swallows real errors | Recorder records then **re-raises** (mirrors `GPTRecorder`). |
| Sensitive data on disk | Debug-only, documented; never enable `debug` in prod; log stream stays redacted. |
| Concurrency | One dumper per `_run` call (request-scoped); subject set per call; `_gpt_seq` lock already present. |
| Big evidence lists slow to serialize | Only in debug; acceptable for a diagnostic run. |

---

## 8. Out of scope (this change)
- Streaming/real-time log shipping or a UI viewer.
- Redacting dumps (debug-only artifact by design).
- Changing the production INFO log format or the existing `TraceRecord`/`TraceStore`
  (they remain; dumps are richer and complementary).
- Per-user audit logging (waits for auth, per SECURITY.md).

---

## 9. Acceptance criteria
1. `DEBUG=true` + a query → `logs/debug/fie/<trace_id>/` with artifacts 00–13 (per intent)
   in correct formats, plus `gpt/` when an LLM is configured.
2. `DEBUG=false` → no files written, byte-identical answers, full suite green.
3. New tests cover on/off, format, round-trip, and LLM capture.
4. No regression in the existing 404-test suite.
