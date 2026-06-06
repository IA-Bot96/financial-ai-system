"""Tests for DEBUG-mode artifact dumping."""
import json

from app.core.debug import DebugDumper, GPTRecorder
from app.engines.extraction.models.insight import InsightList


def test_disabled_dumper_writes_nothing(tmp_path):
    d = DebugDumper(None)              # disabled
    d.subject("millat-2025")
    d.json("01_ingest", {"a": 1})
    d.text("note", "hello")
    assert not any(tmp_path.iterdir())  # nothing written anywhere


def test_dumper_writes_named_files_per_subject(tmp_path):
    d = DebugDumper(tmp_path)
    d.subject("millat-2025")
    d.json("02_tables", {"tables": [1, 2, 3]})
    d.text("01_pages", "page text")

    sub = tmp_path / "millat_2025"
    assert (sub / "02_tables.json").exists()
    assert json.loads((sub / "02_tables.json").read_text())["tables"] == [1, 2, 3]
    assert (sub / "01_pages.txt").read_text() == "page text"


def test_dumper_serializes_pydantic(tmp_path):
    d = DebugDumper(tmp_path).subject("r")
    d.json("03_interpret", InsightList())
    data = json.loads((tmp_path / "r" / "03_interpret.json").read_text())
    assert data == {"insights": []}


def test_gpt_recorder_captures_request_and_response(tmp_path):
    class StubGPT:
        def complete_structured(self, system, user, schema, images=None):
            return InsightList()

    d = DebugDumper(tmp_path).subject("millat-2025")
    rec = GPTRecorder(StubGPT(), d)
    out = rec.complete_structured("SYS", "USER-PROMPT", InsightList)
    assert isinstance(out, InsightList)

    gpt_dir = tmp_path / "millat_2025" / "gpt"
    req = gpt_dir / "001_insightlist_req.txt"
    resp = gpt_dir / "001_insightlist_resp.json"
    assert "USER-PROMPT" in req.read_text() and "SYS" in req.read_text()
    assert json.loads(resp.read_text()) == {"insights": []}


def test_gpt_recorder_records_request_even_on_failure(tmp_path):
    class BoomGPT:
        def complete_structured(self, *a, **k):
            raise RuntimeError("api down")

    d = DebugDumper(tmp_path).subject("r")
    rec = GPTRecorder(BoomGPT(), d)
    try:
        rec.complete_structured("SYS", "USER", InsightList)
    except RuntimeError:
        pass
    gpt_dir = tmp_path / "r" / "gpt"
    assert (gpt_dir / "001_insightlist_req.txt").exists()       # request captured
    assert json.loads((gpt_dir / "001_insightlist_resp.json").read_text())["error"] == "api down"


def test_gpt_capture_can_be_disabled(tmp_path):
    class StubGPT:
        def complete_structured(self, system, user, schema, images=None):
            return InsightList()

    d = DebugDumper(tmp_path, dump_gpt=False).subject("r")
    GPTRecorder(StubGPT(), d).complete_structured("s", "u", InsightList)
    assert not (tmp_path / "r" / "gpt").exists()
