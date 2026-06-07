"""In-memory async job store (no DB). Heavy extraction runs in a thread pool."""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.engines.extraction.pipeline.orchestrator import process_reports

logger = get_logger(__name__)


class JobCancelledError(BaseException):
    """Raised from the progress callback to abort a run when the user cancels.

    Subclasses BaseException (NOT Exception) on purpose: the engine wraps every progress
    callback in `except Exception` (best-effort, never break a run), so an ordinary
    exception would be swallowed and never reach `_run`. A BaseException slips past those
    guards and through the per-report `except Exception` isolation, unwinding straight to
    `_run` — so cancellation needs ZERO engine changes."""


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


# Coarse pct per stage (monotonic) so the UI can show a progress bar without the engine
# knowing about percentages. Stages match the orchestrator/interpret progress events.
_STAGE_PCT = {
    "queued": 0, "running": 1, "ingesting": 8, "ingested": 20, "detecting_tables": 30,
    "extracting": 45, "extracting_insights": 55, "interpreted": 60, "merging": 70,
    "mapping": 82, "validating": 92, "finalizing": 97, "done": 100, "failed": 100,
}


class JobProgress(BaseModel):
    """Live per-run progress, updated by the engine's progress callback (best-effort)."""
    stage: str = "queued"                              # current overall stage
    pct: int = 0                                       # coarse 0-100, monotonic
    pdfs: dict[str, str] = Field(default_factory=dict)  # filename -> current per-PDF stage
    detail: Optional[str] = None
    updated_at: float = 0.0


class Job(BaseModel):
    id: str
    status: JobStatus = JobStatus.queued
    company: Optional[str] = None
    input_files: list[str] = Field(default_factory=list)
    template_file: Optional[str] = None
    output_file: Optional[str] = None
    mode: Optional[str] = None
    message: Optional[str] = None
    # Live progress (per-PDF + company stages) for the status/SSE API.
    progress: JobProgress = Field(default_factory=JobProgress)
    # Observability (#8): validation outcome exposed via the job-status API.
    production_ready: Optional[bool] = None
    fully_reconciled: Optional[bool] = None
    validation_failures: Optional[int] = None
    detail_incomplete: Optional[int] = None
    withheld: Optional[int] = None
    quarantined: Optional[int] = None
    manifest_file: Optional[str] = None
    # Sheet -> source-PDF page map for a side-by-side viewer (sheet -> [{report_file,
    # pages, table_ids, weight}]). Lets the UI jump the PDF when the user switches sheets.
    sheet_sources: dict = Field(default_factory=dict)


_JOBS: dict[str, Job] = {}
# Cancel signals, parallel to _JOBS (a threading.Event can't live on a Pydantic model).
_CANCELS: dict[str, threading.Event] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
# Override in tests to run synchronously / stub the heavy pipeline.
_RUNNER = process_reports

_TERMINAL = {JobStatus.done, JobStatus.failed, JobStatus.cancelled}


def create_job(input_files: list[str], template_file: str | None, company: str | None) -> Job:
    job = Job(id=uuid.uuid4().hex, input_files=input_files, template_file=template_file, company=company)
    _JOBS[job.id] = job
    _CANCELS[job.id] = threading.Event()
    return job


def get_job(job_id: str) -> Job | None:
    return _JOBS.get(job_id)


def cancel_job(job_id: str) -> bool:
    """Signal a running job to stop. Returns False if the job is unknown or already
    terminal (done/failed/cancelled). The running thread notices the signal at the next
    progress-callback boundary and settles the status to `cancelled`."""
    job = _JOBS.get(job_id)
    if job is None or job.status in _TERMINAL:
        return False
    ev = _CANCELS.get(job_id)
    if ev is not None:
        ev.set()
    return True


def submit(job: Job, pdf_paths: list[Path], output_path: Path, template_path: Path | None) -> None:
    _EXECUTOR.submit(_run, job, pdf_paths, output_path, template_path)


def _run(job: Job, pdf_paths: list[Path], output_path: Path, template_path: Path | None) -> None:
    job.status = JobStatus.running
    job.progress.stage = "running"
    job.progress.pct = _STAGE_PCT["running"]
    cancel = _CANCELS.get(job.id)

    def _on_progress(event: dict) -> None:
        # Called from the worker thread as the engine advances. Best-effort: never raise
        # from the progress UPDATE...
        try:
            stage = event.get("stage") or job.progress.stage
            job.progress.stage = stage
            pdf = event.get("pdf")
            if pdf:
                job.progress.pdfs[pdf] = stage
            job.progress.pct = max(job.progress.pct, _STAGE_PCT.get(stage, job.progress.pct))
            job.progress.detail = event.get("message")
            job.progress.updated_at = time.time()
        except Exception:  # noqa: BLE001
            pass
        # ...but DO honour a cancel request here (the only place cancellation is checked).
        # JobCancelledError is a BaseException, so it unwinds past the engine's progress
        # and per-report `except Exception` guards straight up to `_run`.
        if cancel is not None and cancel.is_set():
            raise JobCancelledError()

    try:
        result = _RUNNER(
            pdf_paths, output_path, template_path=template_path, company=job.company,
            progress=_on_progress,
        )
        job.output_file = result.output_path
        job.mode = result.mode
        job.production_ready = result.production_ready
        job.fully_reconciled = result.fully_reconciled
        job.validation_failures = result.validation_failures
        job.detail_incomplete = result.detail_incomplete
        job.withheld = result.withheld
        job.quarantined = result.quarantined
        job.manifest_file = result.manifest_path
        job.sheet_sources = result.sheet_sources
        job.status = JobStatus.done
        job.progress.stage = "done"
        job.progress.pct = 100
        job.progress.updated_at = time.time()
    except JobCancelledError:
        logger.info("Job %s cancelled by user", job.id)
        job.status = JobStatus.cancelled
        job.message = "Cancelled by user"
        job.progress.stage = "cancelled"
        job.progress.detail = job.message
        job.progress.updated_at = time.time()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s failed", job.id)
        job.status = JobStatus.failed
        job.message = str(exc) or exc.__class__.__name__
        job.progress.stage = "failed"
        job.progress.detail = job.message
        job.progress.updated_at = time.time()
