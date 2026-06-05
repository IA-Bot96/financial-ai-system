"""In-memory async job store (no DB). Heavy extraction runs in a thread pool."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.engines.extraction.pipeline.orchestrator import process_reports

logger = get_logger(__name__)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Job(BaseModel):
    id: str
    status: JobStatus = JobStatus.queued
    company: Optional[str] = None
    input_files: list[str] = Field(default_factory=list)
    template_file: Optional[str] = None
    output_file: Optional[str] = None
    mode: Optional[str] = None
    message: Optional[str] = None
    # Observability (#8): validation outcome exposed via the job-status API.
    production_ready: Optional[bool] = None
    validation_failures: Optional[int] = None
    withheld: Optional[int] = None
    quarantined: Optional[int] = None
    manifest_file: Optional[str] = None


_JOBS: dict[str, Job] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
# Override in tests to run synchronously / stub the heavy pipeline.
_RUNNER = process_reports


def create_job(input_files: list[str], template_file: str | None, company: str | None) -> Job:
    job = Job(id=uuid.uuid4().hex, input_files=input_files, template_file=template_file, company=company)
    _JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return _JOBS.get(job_id)


def submit(job: Job, pdf_paths: list[Path], output_path: Path, template_path: Path | None) -> None:
    _EXECUTOR.submit(_run, job, pdf_paths, output_path, template_path)


def _run(job: Job, pdf_paths: list[Path], output_path: Path, template_path: Path | None) -> None:
    job.status = JobStatus.running
    try:
        result = _RUNNER(
            pdf_paths, output_path, template_path=template_path, company=job.company,
        )
        job.output_file = result.output_path
        job.mode = result.mode
        job.production_ready = result.production_ready
        job.validation_failures = result.validation_failures
        job.withheld = result.withheld
        job.quarantined = result.quarantined
        job.manifest_file = result.manifest_path
        job.status = JobStatus.done
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s failed", job.id)
        job.status = JobStatus.failed
        job.message = str(exc) or exc.__class__.__name__
