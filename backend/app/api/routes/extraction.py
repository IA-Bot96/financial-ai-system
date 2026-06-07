"""Extraction API: upload reports (+ optional template) -> job -> download xlsx."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import get_settings
from app.core.security import UploadRejected, assert_safe_upload
from app.engines.extraction.services.storage import Storage
from app.workers import jobs

router = APIRouter(prefix="/extraction", tags=["extraction"])
storage = Storage()

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _read_checked(f: UploadFile, *, kinds) -> bytes:
    """Read an upload fully and run the safety gate before it is persisted/parsed."""
    data = await f.read()
    s = get_settings()
    max_bytes = s.max_excel_upload_bytes if "xlsx" in kinds else s.max_pdf_upload_bytes
    try:
        assert_safe_upload(f.filename or "", data, max_bytes=max_bytes, kinds=kinds)
    except UploadRejected as e:
        raise HTTPException(status_code=400, detail=f"Rejected {f.filename!r}: {e}")
    return data


@router.post("/jobs")
async def create_extraction_job(
    files: list[UploadFile] = File(..., description="One or more annual report PDFs"),
    template: UploadFile | None = File(None, description="Optional Excel template"),
    company: str | None = Form(None),
):
    if not files:
        raise HTTPException(status_code=400, detail="At least one PDF is required.")
    if company is not None and len(company) > 128:
        raise HTTPException(status_code=400, detail="company too long")

    # validate every upload (size / extension / magic / zip-bomb / macros) BEFORE save
    pdf_data = [await _read_checked(f, kinds=("pdf",)) for f in files]
    template_data = await _read_checked(template, kinds=("xlsx",)) if template else None

    job = jobs.create_job(
        input_files=[f.filename for f in files],
        template_file=template.filename if template else None,
        company=company,
    )
    pdf_paths = [storage.save_input(job.id, f.filename, data)
                 for f, data in zip(files, pdf_data)]
    template_path = (
        storage.save_template(job.id, template.filename, template_data) if template else None
    )
    output_path = storage.output_path(job.id)
    jobs.submit(job, pdf_paths, output_path, template_path)
    return {"job_id": job.id, "status": job.status}


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.delete("/jobs/{job_id}", status_code=200)
async def cancel_extraction_job(job_id: str):
    """Signal a running job to stop. Idempotent: a job that's already terminal returns its
    current status. The run settles to `cancelled` at its next progress-callback boundary,
    so the client keeps polling GET /jobs/{id} until status is `cancelled` (or `failed`)."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status in (jobs.JobStatus.done, jobs.JobStatus.failed, jobs.JobStatus.cancelled):
        return {"job_id": job_id, "status": job.status}      # already terminal -> idempotent
    jobs.cancel_job(job_id)
    return {"job_id": job_id, "status": "cancelling"}


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str):
    """Server-Sent Events: stream live status as the run advances. Each message is the
    job's progress + status; emitted only when something changes; closes on done/failed.
    (Polling GET /jobs/{id} also returns the same `progress` — SSE is the push variant.)"""
    if jobs.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def gen():
        last = None
        while True:
            j = jobs.get_job(job_id)
            if j is None:
                break
            snap = {"status": j.status.value, **j.progress.model_dump()}
            key = (snap["status"], snap["stage"], snap["pct"], tuple(sorted(snap["pdfs"].items())))
            if key != last:
                last = key
                yield f"data: {json.dumps(snap, default=str)}\n\n"
            if j.status in (jobs.JobStatus.done, jobs.JobStatus.failed):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/jobs/{job_id}/download")
async def download_result(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != jobs.JobStatus.done or not job.output_file:
        raise HTTPException(status_code=409, detail=f"Job not ready (status={job.status}).")
    return FileResponse(job.output_file, media_type=_XLSX_MIME, filename=f"{job_id}.xlsx")
