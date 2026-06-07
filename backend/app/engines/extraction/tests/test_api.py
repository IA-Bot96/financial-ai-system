"""API smoke tests (FastAPI TestClient) with a stubbed pipeline runner."""
import openpyxl
import pytest

pytest.importorskip("multipart")  # python-multipart required for file uploads
from fastapi.testclient import TestClient  # noqa: E402

from app.engines.extraction.models.company import CompanyResult  # noqa: E402
from app.engines.extraction.pipeline.orchestrator import ExtractionOutput  # noqa: E402
from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402
from app.workers import jobs  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    # The app enables an in-memory rate limiter (burst=3). A test file fires far more
    # than 3 requests, so the shared token bucket would return 429 mid-suite. Neutralize
    # it for tests only — the middleware holds this exact bucket object, so patching its
    # `allow` disables throttling without touching the limiter/config code itself.
    if getattr(main_module, "_limiter", None) is not None:
        monkeypatch.setattr(main_module._limiter, "allow", lambda key: True)

    def stub_runner(pdf_paths, output_path, template_path=None, company=None, progress=None):
        if progress:                       # exercise the progress callback contract
            progress({"stage": "ingesting", "total": len(pdf_paths)})
            progress({"stage": "done"})
        wb = openpyxl.Workbook()
        wb.active["A1"] = "stub"
        wb.save(output_path)
        return ExtractionOutput(output_path=str(output_path), company=CompanyResult(), mode="no_template")

    # Run synchronously so status is deterministic in the test.
    monkeypatch.setattr(jobs, "_RUNNER", stub_runner)
    monkeypatch.setattr(jobs, "submit", lambda job, pdfs, out, tpl: jobs._run(job, pdfs, out, tpl))
    return TestClient(app)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_job_lifecycle(client):
    resp = client.post(
        "/api/extraction/jobs",
        files=[("files", ("r2025.pdf", b"%PDF-1.4 fake", "application/pdf"))],
        data={"company": "Acme"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status = client.get(f"/api/extraction/jobs/{job_id}").json()
    assert status["status"] == "done" and status["mode"] == "no_template"
    # live progress is populated and terminal after a synchronous run
    assert status["progress"]["stage"] == "done" and status["progress"]["pct"] == 100

    dl = client.get(f"/api/extraction/jobs/{job_id}/download")
    assert dl.status_code == 200
    assert "spreadsheetml" in dl.headers["content-type"]


def test_missing_job_404(client):
    assert client.get("/api/extraction/jobs/nope").status_code == 404


def test_cancel_unknown_job_404(client):
    assert client.delete("/api/extraction/jobs/nope").status_code == 404


def test_cancel_running_returns_cancelling(client):
    job = jobs.create_job(["a.pdf"], None, None)
    job.status = jobs.JobStatus.running                     # simulate an in-flight job
    r = client.delete(f"/api/extraction/jobs/{job.id}")
    assert r.status_code == 200 and r.json()["status"] == "cancelling"
    assert jobs._CANCELS[job.id].is_set()                   # signal was set


def test_cancel_terminal_is_idempotent(client):
    job = jobs.create_job(["a.pdf"], None, None)
    job.status = jobs.JobStatus.done
    r = client.delete(f"/api/extraction/jobs/{job.id}")
    assert r.status_code == 200 and r.json()["status"] == "done"   # unchanged
    assert jobs.cancel_job(job.id) is False                        # no-op on terminal


def test_cancel_signal_aborts_run_via_progress(monkeypatch):
    # Mechanism: a cancel set mid-run is noticed at the next progress callback, and the
    # run settles to `cancelled` (not `failed`) — proving the BaseException unwinds to _run.
    from pathlib import Path
    job = jobs.create_job(["a.pdf"], None, "X")
    reached_end = {"v": False}

    def runner(pdf_paths, output_path, template_path=None, company=None, progress=None):
        jobs.cancel_job(job.id)                 # user cancels
        progress({"stage": "ingesting"})        # next callback notices -> raises
        reached_end["v"] = True                 # must NOT run
        return None

    monkeypatch.setattr(jobs, "_RUNNER", runner)
    jobs._run(job, [Path("a.pdf")], Path("out.xlsx"), None)
    assert job.status == jobs.JobStatus.cancelled
    assert job.message == "Cancelled by user" and job.progress.stage == "cancelled"
    assert reached_end["v"] is False            # run was actually interrupted
