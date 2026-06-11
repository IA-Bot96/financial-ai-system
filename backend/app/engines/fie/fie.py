"""Financial Intelligence Engine.

Orchestrates answering a query over the uploaded workbook plus external sources
(PSX / news / open web) through the LLM-first controller (PLAN -> FETCH -> COMPOSE
-> VERIFY). External fetches degrade gracefully: the answer proceeds on whatever
evidence was gathered and the controller's verifier gates any unbacked figure.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta

from . import insights as insights_mod
from .apis import ExternalSources
from .calc import CalcEngine
from .llm import NullLLM
from .models import Response, SourcePlan
from .store import FinancialFactStore
from .trace import TraceRecord
from . import controller

_log = logging.getLogger("app.engines.fie")

# --- edit_history query parsing (temporal / sheet filters) ---------------------------------
_HIST_WIN_RE = re.compile(r"\b(?:last|past|within|in|over|in the last|in the past|over the last)\s+"
                          r"(\d{1,4})\s*(min|minute|hour|hr|day|week)s?\b", re.I)
_HIST_LIMIT_RE = re.compile(r"\b(?:last|recent|latest)\s+(\d{1,3})\b", re.I)
_HIST_ONE_RE = re.compile(r"\b(?:last|latest|recent|most recent)\s+"
                          r"(?:change|edit|modification|update)\b", re.I)
_HIST_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_HIST_DATE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?\s+(\d{4})\b", re.I)
_HIST_DATE_MDY = re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
_HIST_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_HIST_DATE_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _hist_parse_dt(s):
    """Tolerant timestamp parse for History-sheet / pending-edit rows (-> datetime|None)."""
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    s2 = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s2, fmt)
        except ValueError:
            continue
    return None


def _hist_parse_query_date(q):
    """Parse an explicit calendar date out of a query ('31 Aug 2025', 'Aug 31 2025',
    '2025-08-31', '31/08/2025') -> datetime|None (day precision)."""
    m = _HIST_DATE_ISO.search(q)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    m = _HIST_DATE_DMY.search(q)
    if m and m.group(2).lower()[:3] in _HIST_MONTHS:
        try:
            return datetime(int(m.group(3)), _HIST_MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
        except ValueError:
            pass
    m = _HIST_DATE_MDY.search(q)
    if m and m.group(1).lower()[:3] in _HIST_MONTHS:
        try:
            return datetime(int(m.group(3)), _HIST_MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        except ValueError:
            pass
    m = _HIST_DATE_SLASH.search(q)
    if m:
        try:
            return datetime(int(m[3]), int(m[2]), int(m[1]))   # d/m/y
        except ValueError:
            pass
    return None


class FinancialIntelligenceEngine:
    def __init__(self, store: FinancialFactStore, *, llm=None,
                 external: ExternalSources | None = None,
                 insight_mode: str = "year_then_confidence", alpha: float = 0.7,
                 trace_id_factory=None) -> None:
        self.store = store
        self.llm = llm or NullLLM()
        self.external = external or ExternalSources()
        self._trace_id = trace_id_factory or (lambda: uuid.uuid4().hex[:16])
        self.calc = CalcEngine(store)
        self.insights = insights_mod.InsightSelector(mode=insight_mode, alpha=alpha,
                                                     llm=self.llm)

    def answer(self, query: str, *, audience: str = "analyst",
               history: list[dict] | None = None, now: str | None = None,
               pending_edits: list[dict] | None = None) -> Response:
        resp, _frame, _ev = controller.run(
            self, query, audience=audience, history=history or [],
            pending_edits=pending_edits or [], now=now)
        return resp

    def answer_with_trace(self, query: str, *, audience: str = "analyst",
                          history: list[dict] | None = None, now: str | None = None,
                          pending_edits: list[dict] | None = None) -> tuple[Response, TraceRecord]:
        resp, frame, evidence = controller.run(
            self, query, audience=audience, history=history or [],
            pending_edits=pending_edits or [], now=now)
        trace = TraceRecord(
            trace_id=getattr(self, "_last_trace_id", None) or self._trace_id(),
            query=query, audience=audience,
            company=frame.company, frame=frame, plan=SourcePlan(),
            evidence=evidence, response=resp)
        return resp, trace

    def _h_edit_history(self, frame, ctx, plan) -> None:
        """Answer questions about the user's own edits, from the workbook's History log
        (saved changes) merged with the client's pending/unsaved edits. Filters parsed
        deterministically from the query: unsaved-only, this-session, last-N-minutes/hours/days,
        a specific date, a sheet, and a result limit. Purely a listing — no financial evidence,
        so it never touches the numeric/citation/confidence machinery."""
        q = frame.raw_query or ""
        now = _hist_parse_dt(ctx.now) or datetime.now()

        # Merge the saved log (from the uploaded workbook's History sheet — may be empty or hold
        # PRIOR-session rows, since a file can be opened many times) with the client's unsaved
        # edits. Dedupe by (timestamp, sheet, cell): a re-uploaded file already contains rows a
        # stale pending buffer might resend — the saved copy wins so nothing is counted twice.
        entries: list[dict] = []
        seen: set = set()
        for e in (self.store.history or []):                    # saved log (History sheet)
            seen.add((str(e.get("timestamp") or ""), str(e.get("sheet") or ""),
                      str(e.get("cell") or "")))
            entries.append({**e, "_dt": _hist_parse_dt(e.get("timestamp")),
                            "saved": bool(e.get("saved"))})
        for e in (ctx.pending_edits or []):                     # unsaved edits from the client
            sheet = str(e.get("sheet") or "")
            key = (str(e.get("timestamp") or ""), sheet, str(e.get("cell") or ""))
            if key in seen:
                continue                                        # already saved in the file
            seen.add(key)
            entries.append({
                "timestamp": str(e.get("timestamp") or ""), "sheet": sheet,
                "cell": str(e.get("cell") or ""),
                "old": "" if e.get("old") is None else str(e.get("old")),
                "new": "" if e.get("new") is None else str(e.get("new")),
                "saved": False,
                # the app writes the "workbook opened" marker into the unsaved buffer FIRST
                # (persisted on save), so a session marker can arrive here before it's in the
                # file — tag it so it still bounds "this session".
                "event": "session" if sheet.lower() in ("(session)", "session") else None,
                "_dt": _hist_parse_dt(e.get("timestamp")) or now})

        # "this session" boundary = the latest session-open marker from EITHER source. Taking the
        # max makes prior-session markers in a re-uploaded file harmless (the current open is newest).
        starts = [x["_dt"] for x in entries if x.get("event") == "session" and x.get("_dt")]
        open_markers = [x for x in entries if x.get("event") == "session"]   # one per workbook open
        session_start = max(starts) if starts else None
        changes = [x for x in entries if x.get("event") != "session"]   # drop session markers
        # Drop the app-managed "Manually Verified" COLUMN header write (Validation Ledger, new
        # == "Manually Verified") — that's schema the app adds, not an edit the user made.
        changes = [x for x in changes
                   if not (x["sheet"].strip().lower() == "validation ledger"
                           and str(x.get("new", "")).strip().lower() == "manually verified")]

        applied: list[str] = []
        if re.search(r"\bunsaved\b", q, re.I):
            changes = [x for x in changes if not x["saved"]]
            applied.append("unsaved")
        if re.search(r"\b(this|current) session\b", q, re.I):
            if session_start is not None:
                changes = [x for x in changes if x["_dt"] and x["_dt"] >= session_start]
            else:
                # no open-marker known (file had none yet) — only the live unsaved edits are
                # certainly from this session; prior saved rows can't be attributed to it.
                changes = [x for x in changes if not x["saved"]]
            applied.append("this session")
        win = _HIST_WIN_RE.search(q)
        if win:
            n, unit = int(win.group(1)), win.group(2).lower()
            delta = {"min": timedelta(minutes=n), "minute": timedelta(minutes=n),
                     "hour": timedelta(hours=n), "hr": timedelta(hours=n),
                     "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
            cutoff = now - delta
            changes = [x for x in changes if x["_dt"] and x["_dt"] >= cutoff]
            applied.append(f"last {n} {unit}{'s' if n != 1 else ''}")
        qd = _hist_parse_query_date(q)
        if qd:
            changes = [x for x in changes if x["_dt"] and x["_dt"].date() == qd.date()]
            applied.append(qd.strftime("%Y-%m-%d"))
        elif (yr_m := re.search(r"\b(19|20)\d{2}\b", q)) and not win:
            # bare calendar-year filter: "changes in 2026", "in year 1901" -> that year only
            yv = int(yr_m.group(0))
            changes = [x for x in changes if x["_dt"] and x["_dt"].year == yv]
            applied.append(str(yv))
        matched_sheets, requested_sheet = self._history_sheet_filter(q, changes)
        if requested_sheet is not None:   # a sheet was NAMED — filter to it even if that yields none
            _keep = {s.lower() for s in matched_sheets}
            changes = [x for x in changes if x["sheet"].lower() in _keep]
            applied.append(requested_sheet)

        changes.sort(key=lambda x: (x["_dt"] or datetime.min), reverse=True)   # newest first
        fstr = (" (" + ", ".join(applied) + ")") if applied else ""

        # MODE (checked in order; open_count BEFORE aggregate since "how many" matches both):
        #   open_count : "how many times was it opened/loaded" -> count of workbook-open markers
        #   opened     : "when was it opened/loaded"           -> the session-open time
        #   aggregate  : "how many changes / which sheet / most" -> per-sheet change counts
        #   list       : everything else (supports first/oldest and last/N limits)
        open_count = bool(re.search(r"\b(how many|how often|number of|count)\b[^?]*"
                                    r"\b(open|load|upload)\w*", q, re.I))
        asks_opened = bool(re.search(r"\b(when|what time|at what time)\b[^?]*\b(open|load|upload)\w*",
                                     q, re.I))
        aggregate = bool(re.search(
            r"\b(how many|number of|count of|how often|per sheet|by sheet|each sheet|across sheets?)\b"
            r"|\bwhich sheets?\b|\bmost\s+(change|edit|modif|update)", q, re.I))

        # manual-verification ask: "which cells did I mark as manually verified?" — list the cells
        # CURRENTLY marked verified (net of later clears), not every edit. A Validation-Ledger
        # toggle stores a boolean-ish new value: true/1/yes = verified, anything else = cleared
        # (mirrors the frontend's render). Net state = the latest toggle per ledger cell.
        asks_verified = bool(re.search(
            r"manually verified|verified cells?|which\b[^?]*\bverif|mark\w*\b[^?]*\bverif", q, re.I))

        eh: dict = {"filters": applied, "total": len(changes),
                    "now": now.isoformat(timespec="seconds"),
                    "session_known": session_start is not None}
        shown_n = 0
        if asks_verified:
            eh["mode"] = "list"   # reuse list rendering (frontend-compatible)
            vrows = [x for x in changes if x["sheet"].strip().lower() == "validation ledger"]
            latest: dict = {}
            for x in sorted(vrows, key=lambda r: r["_dt"] or datetime.min):
                latest[x["cell"]] = x            # keep the most recent toggle per ledger cell
            verified = [x for x in latest.values()
                        if str(x.get("new", "")).strip().lower() in ("true", "1", "yes")]
            verified.sort(key=lambda x: (x["_dt"] or datetime.min), reverse=True)
            items = []
            for x in verified:
                vs, vc = self._mv_verified_ref(x["cell"])
                items.append({"timestamp": x["timestamp"], "sheet": x["sheet"], "cell": x["cell"],
                              "old": x["old"], "new": x["new"], "saved": x["saved"],
                              "kind": "verify", "verified_sheet": vs, "verified_cell": vc})
            eh["items"], eh["shown"], eh["total"] = items, len(items), len(items)
            shown_n = len(items)
            if items:
                refs = ", ".join((f"{it['verified_sheet']}/" if it.get("verified_sheet") else "")
                                 + (it.get("verified_cell") or it["cell"]) for it in items)
                eh["lead"] = f"You have {len(items)} cell(s) currently marked as manually verified: {refs}."
            else:
                eh["lead"] = ("No cells are currently marked as manually verified"
                              + (" — any earlier marks were later cleared." if vrows else "."))
        elif open_count:
            eh["mode"] = "open_count"
            n = len(open_markers)
            eh["open_count"] = n
            eh["opens"] = [x["timestamp"] for x in
                           sorted(open_markers, key=lambda x: (x["_dt"] or datetime.min), reverse=True)]
            eh["lead"] = (f"This workbook has been opened {n} time(s) in this app."
                          if n else "I don't have a record of this workbook being opened in this app yet.")
        elif asks_opened:
            eh["mode"] = "opened"
            oa = session_start.isoformat(timespec="seconds") if session_start else None
            eh["opened_at"] = oa
            eh["lead"] = (f"This workbook was opened at {oa} (this session)." if oa
                          else "I don't have a record of when this workbook was opened this session.")
        elif aggregate:
            eh["mode"] = "aggregate"
            by: dict[str, int] = {}
            for x in changes:
                s = x.get("sheet")
                if s:
                    by[s] = by.get(s, 0) + 1
            ordered = sorted(by.items(), key=lambda kv: kv[1], reverse=True)
            eh["by_sheet"] = dict(ordered)
            eh["most"] = list(ordered[0]) if ordered else None
            if ordered:
                m = ordered[0]
                eh["lead"] = (f"You made {len(changes)} change(s) across {len(by)} sheet(s){fstr}. "
                              f"Most changes: {m[0]} ({m[1]} change{'s' if m[1] != 1 else ''}).")
            else:
                eh["lead"] = f"No changes recorded{fstr}."
        else:
            eh["mode"] = "list"
            # "first/earliest/oldest change" -> the single OLDEST change. But "newest/oldest
            # FIRST" is an ORDERING directive (ascending vs descending), NOT a request for the
            # first change — don't let that trailing "first" trip the single-change path.
            # `changes` is already sorted newest-first.
            order_newest = bool(re.search(
                r"\b(newest|latest|most recent|recent)\s+first\b|\breverse[\s-]?chronologic", q, re.I))
            order_oldest = (not order_newest) and bool(re.search(
                r"\b(oldest|earliest)\s+first\b|\bchronologic", q, re.I))
            ordering = order_newest or order_oldest
            asks_first = (not ordering) and bool(re.search(
                r"\b(earliest|oldest)\b|\bfirst\s+(change|edit|modification|update|one|thing)\b"
                r"|\b(my|the)\s+first\b", q, re.I))
            asks_all = bool(re.search(r"\b(all|every|each|entire|complete|full)\b", q, re.I))
            lm = _HIST_LIMIT_RE.search(q)
            if asks_first:
                ordered_changes = list(reversed(changes))   # the single oldest change
                limit = 1
            else:
                ordered_changes = list(reversed(changes)) if order_oldest else changes
                limit = (max(1, int(lm.group(1))) if lm
                         else len(changes) if (asks_all or ordering)
                         else (1 if _HIST_ONE_RE.search(q) else 20))
            shown = ordered_changes[:limit]
            shown_n = len(shown)
            items = []
            for x in shown:
                is_verify = x["sheet"].strip().lower() == "validation ledger"
                item = {"timestamp": x["timestamp"], "sheet": x["sheet"], "cell": x["cell"],
                        "old": x["old"], "new": x["new"], "saved": x["saved"],
                        "kind": "verify" if is_verify else "edit"}
                if is_verify:   # map the ledger row to the financial cell it verifies
                    vs, vc = self._mv_verified_ref(x["cell"])
                    item["verified_sheet"], item["verified_cell"] = vs, vc
                items.append(item)
            eh["items"] = items
            eh["shown"] = shown_n
            if not items:
                eh["lead"] = ("No changes have been recorded for this workbook yet."
                              if (eh["total"] == 0 and not applied) else f"No matching changes found{fstr}.")
            elif asks_first and shown_n == 1:
                eh["lead"] = f"Your first change{fstr}:"
            elif shown_n == 1:
                eh["lead"] = f"Your most recent change{fstr}:"
            else:
                eh["lead"] = (f"{len(changes)} change(s){fstr}"
                              + (f"; showing {shown_n}:" if shown_n < len(changes) else ":"))

        ctx.evidence = []
        ctx.extra = {"edit_history": eh}
        _log.info("fie edit_history: mode=%s total=%d shown=%d filters=%s saved_log=%d pending=%d "
                  "session_start=%s now=%s", eh["mode"], len(changes), shown_n, applied,
                  len(self.store.history or []), len(ctx.pending_edits or []),
                  session_start.isoformat(timespec="seconds") if session_start else None,
                  now.isoformat(timespec="seconds"), extra={"component": "Respond"})

    def _mv_verified_ref(self, cell: str):
        """For a 'Manually Verified' checkbox write at Validation Ledger!<col><row>, resolve the
        financial (sheet, cell) that ledger row refers to. Best-effort -> (sheet|None, cell|None)."""
        m = re.search(r"(\d+)\s*$", cell or "")
        df = getattr(self.store, "validation_ledger", None)
        if not m or df is None or getattr(df, "empty", True):
            return None, None
        idx = int(m.group(1)) - 2   # ledger header is row 1 -> first data row (row 2) is df index 0
        if idx < 0 or idx >= len(df):
            return None, None
        def _col(*names):
            for c in df.columns:
                if any(n in str(c).strip().lower() for n in names):
                    return c
            return None
        sc, cc = _col("sheet"), _col("cell")
        row = df.iloc[idx]
        vs = str(row[sc]).strip() if sc is not None and row[sc] is not None else None
        vc = str(row[cc]).strip() if cc is not None and row[cc] is not None else None
        return vs, vc

    def _history_sheet_filter(self, q: str, changes: list[dict]) -> tuple[list[str], str | None]:
        """Resolve a sheet/area mentioned in the query. Returns (matched_sheets, requested_label).

        requested_label is non-None whenever the query NAMES a sheet or statement area — even if
        NO edit touched it — so the caller reports "no changes in <sheet>" instead of silently
        dropping the filter and dumping every change. matched_sheets are the edited sheets to keep
        (a family like 'the balance sheet' can match several, e.g. BS1–BS5)."""
        ql = q.lower()
        edited = sorted({x["sheet"] for x in changes if x.get("sheet")})
        wb = list(self.store.sheet_names or [])
        requested: str | None = None
        matched: set[str] = set()

        # (a) an explicit sheet name — an edited sheet OR any real workbook tab — appears verbatim
        named = sorted((s for s in set(edited) | set(wb) if len(s) >= 3 and s.lower() in ql),
                       key=len, reverse=True)
        if named:
            requested = named[0]
            matched |= {s for s in edited if s.lower() == requested.lower()}

        # (b) a statement-family cue -> every edited sheet in that family (catches 'BS2' under
        # 'balance sheet'). Sets requested even when nothing matched, so we report an empty result.
        families = (
            ("the Balance Sheet", ("balance sheet", "balance-sheet"), r"^bs\d|balance"),
            ("the Income Statement (P&L)", ("p&l", "p and l", "profit and loss", "income statement"),
             r"^pl\d|p&l|profit|income"),
            ("the Cash-Flow statement", ("cash flow", "cashflow", "cash-flow"), r"cash"),
            ("the Statement of Changes in Equity", ("changes in equity", "equity statement"),
             r"equity|share capital"),
        )
        for label, cues, pat in families:
            if any(c in ql for c in cues):
                requested = requested or label
                matched |= {s for s in edited if re.search(pat, s, re.I)}

        return sorted(matched), requested

    def _safe_lookup(self, metric: str, year: int | None):
        if year is None:
            return None
        try:
            return self.store.lookup(metric, year).value
        except KeyError:
            return None
