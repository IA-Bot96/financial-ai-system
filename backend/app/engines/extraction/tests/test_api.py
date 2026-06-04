"""API smoke tests (FastAPI TestClient) with a stubbed pipeline runner."""
import openpyxl
import pytest

pytest.importorskip("multipart")  # python-multipart required for file uploads
from fastapi.testclient import TestClient  # noqa: E402

from app.engines.extraction.models.company import CompanyResult  # noqa: E402
from app.engines.extraction.pipeline.orchestrator import ExtractionOutput  # noqa: E402
from app.main import app  # noqa: E402
from app.workers import jobs  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    def stub_runner(pdf_paths, output_path, template_path=None, company=None):
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

    dl = client.get(f"/api/extraction/jobs/{job_id}/download")
    assert dl.status_code == 200
    assert "spreadsheetml" in dl.headers["content-type"]


def test_missing_job_404(client):
    assert client.get("/api/extraction/jobs/nope").status_code == 404
