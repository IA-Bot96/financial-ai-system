"""FIE DEBUG-mode observability: per-layer artifact dumps (.json/.txt) + per-query
.log, gated on the existing `debug` flag. No-op (and no behavior change) when off."""

import json
import os

import pytest

from app.engines.fie import FinancialIntelligenceEngine, FinancialFactStore
from app.engines.fie.debug_dump import FieLLMRecorder
from app.engines.fie.llm import LLMClient

_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")
_real = pytest.mark.skipif(not os.path.exists(_WB), reason="workbook absent")


@pytest.fixture
def debug_dirs(tmp_path, monkeypatch):
    """Force DEBUG on with dump/log dirs under tmp_path (settings is lru_cached)."""
    from app.core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "debug", True, raising=False)
    monkeypatch.setattr(s, "debug_dump_dir", str(tmp_path / "debug"), raising=False)
    monkeypatch.setattr(s, "log_dir", str(tmp_path / "logs"), raising=False)
    return tmp_path


# --- recorder unit (no workbook) -------------------------------------------
class _FakeLLM:
    def complete_json(self, system, user, schema):
        return {"echo": user}
    def complete_text(self, system, user):
        return "narration"


class _DumpStub:
    def __init__(self):
        self.reqs, self.resps = [], []
    def gpt_request(self, kind, system, user):
        self.reqs.append((kind, system, user))
        return f"gpt/{len(self.reqs):03d}_{kind}"
    def gpt_response(self, base, resp):
        self.resps.append((base, resp))
    def text(self, name, content):
        self.resps.append((name, content))


def test_recorder_is_a_valid_llmclient_and_passes_through():
    rec = FieLLMRecorder(_FakeLLM(), _DumpStub())
    assert isinstance(rec, LLMClient)                       # structural Protocol
    assert rec.complete_json("s", "u", {}) == {"echo": "u"}
    assert rec.complete_text("s", "u") == "narration"


def test_recorder_records_request_before_response_and_reraises():
    class _Boom:
        def complete_json(self, system, user, schema):
            raise RuntimeError("boom")
        def complete_text(self, system, user):
            return "x"
    d = _DumpStub()
    rec = FieLLMRecorder(_Boom(), d)
    with pytest.raises(RuntimeError):
        rec.complete_json("s", "u", {})
    assert d.reqs and d.resps and d.resps[-1][1] == {"error": "boom"}   # recorded then raised


# --- engine wiring: off => nothing written ---------------------------------
@_real
def test_no_dumps_when_debug_off(tmp_path, monkeypatch):
    from app.core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "debug", False, raising=False)
    monkeypatch.setattr(s, "debug_dump_dir", str(tmp_path / "debug"), raising=False)
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB))
    eng.answer("current ratio for MTL 2024")
    assert not (tmp_path / "debug").exists()


# --- engine wiring: on => ordered per-layer artifacts ----------------------
@_real
def test_layer_artifacts_written_in_debug(debug_dirs):
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB))
    eng.answer("current ratio for MTL 2024")

    fie_root = debug_dirs / "debug" / "fie"
    runs = list(fie_root.iterdir())
    assert len(runs) == 1                                    # one trace dir
    run = runs[0]
    for name in ("00_summary.json", "01_frame.json", "02_plan.json",
                 "04_calcs.json", "09_confidence.json", "13_response.json"):
        assert (run / name).exists(), f"missing {name}"

    frame = json.loads((run / "01_frame.json").read_text(encoding="utf-8"))
    assert frame["intent"] == "ratio_analysis" and frame["formula"] == "current_ratio"
    calcs = json.loads((run / "04_calcs.json").read_text(encoding="utf-8"))
    assert calcs and calcs[0]["formula_id"] == "current_ratio"
    summary = json.loads((run / "00_summary.json").read_text(encoding="utf-8"))
    assert summary["intent"] == "ratio_analysis" and "elapsed_ms" in summary

    # per-query .log file was written under the log dir
    logs = list((debug_dirs / "logs").glob("*.log"))
    assert logs, "expected a per-query .log file"


@_real
def test_llm_prompts_captured_when_debug(debug_dirs):
    eng = FinancialIntelligenceEngine(
        FinancialFactStore.from_workbook(_WB), llm=_FakeLLM())
    eng.answer("current ratio for MTL 2024")
    run = next((debug_dirs / "debug" / "fie").iterdir())
    gpt = run / "gpt"
    # narration goes through complete_text -> a text request + .txt response
    assert gpt.exists() and any(gpt.glob("*_req.txt"))
    assert (run / "11_llm_analysis.txt").exists()
