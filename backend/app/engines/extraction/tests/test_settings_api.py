"""Settings API: read/update/reset the user-tweakable engine knobs."""
import pytest
from fastapi.testclient import TestClient

from app import main as main_module  # noqa: E402
from app.core import config as cfg  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Isolate the override file per test. Both the helpers (module global) AND the
    # JsonConfigSettingsSource (reads model_config["json_file"]) must point at it.
    p = tmp_path / "settings.json"
    monkeypatch.setattr(cfg, "SETTINGS_OVERRIDE_PATH", p)
    monkeypatch.setitem(cfg.Settings.model_config, "json_file", str(p))
    cfg.get_settings.cache_clear()
    if getattr(main_module, "_limiter", None) is not None:   # bypass the rate limiter
        monkeypatch.setattr(main_module._limiter, "allow", lambda key: True)
    yield TestClient(app)
    cfg.get_settings.cache_clear()


def _field(body, key):
    return next(f for f in body["fields"] if f["key"] == key)


def test_get_exposes_values_defaults_and_hides_secret_value(client):
    body = client.get("/api/settings").json()
    dpi = _field(body, "ocr_dpi")
    assert dpi["value"] == 200 and dpi["default"] == 200 and dpi["options"] == [150, 200, 300]
    workers = _field(body, "ocr_max_workers")
    assert workers["minimum"] == 1 and workers["maximum"] >= 1
    key = _field(body, "openai_api_key")
    assert key["kind"] == "secret" and "value" not in key and "configured" in key


def test_taxonomy_groups_order_and_placement(client):
    body = client.get("/api/settings").json()
    # ordered groups list, Advanced collapsed
    assert [g["name"] for g in body["groups"]] == [
        "Connection", "Extraction", "Insights", "Validation & Trust", "Performance", "Advanced"]
    assert next(g for g in body["groups"] if g["name"] == "Advanced")["collapsed"] is True
    # key re-placements from the UX review
    assert _field(body, "openai_model")["group"] == "Connection"        # not Credentials
    assert _field(body, "ocr_dpi")["group"] == "Extraction"             # not Performance
    assert _field(body, "ocr_lang")["group"] == "Extraction"            # not Matching
    assert _field(body, "openai_timeout")["group"] == "Advanced"        # not Matching
    assert _field(body, "insights_workers")["group"] == "Performance"   # not Insights
    assert _field(body, "llm_json_temperature")["group"] == "Advanced"  # not Model
    # Vision is a sub-group of Extraction
    v = _field(body, "vision_detail")
    assert v["group"] == "Extraction" and v["subgroup"] == "Vision"
    # Validation review: its own group, BETA badge, default on
    vr = _field(body, "validation_review_enabled")
    assert vr["group"] == "Validation & Trust" and vr["badge"] == "BETA" and vr["value"] is True


def test_validation_review_toggle_persists(client):
    r = client.post("/api/settings", json={"values": {"validation_review_enabled": False}})
    assert r.status_code == 200
    assert _field(r.json(), "validation_review_enabled")["value"] is False
    assert cfg.get_settings().validation_review_enabled is False


def test_update_persists_and_marks_overridden(client):
    r = client.post("/api/settings", json={"values": {"ocr_dpi": 300, "ocr_max_workers": 4}})
    assert r.status_code == 200
    body = r.json()
    assert _field(body, "ocr_dpi")["value"] == 300
    assert _field(body, "ocr_dpi")["overridden"] is True
    # Persisted: a fresh settings load reflects it.
    assert cfg.get_settings().ocr_dpi == 300 and cfg.get_settings().ocr_max_workers == 4


def test_validation_rejects_out_of_range_and_unknown(client):
    assert client.post("/api/settings", json={"values": {"ocr_max_workers": 0}}).status_code == 400
    assert client.post("/api/settings", json={"values": {"ocr_dpi": 999}}).status_code == 400  # not an option
    assert client.post("/api/settings", json={"values": {"not_a_field": 1}}).status_code == 400


def test_secret_is_write_only_then_clearable(client):
    client.post("/api/settings", json={"values": {"openai_api_key": "sk-test-123"}})
    body = client.get("/api/settings").json()
    assert _field(body, "openai_api_key")["configured"] is True
    assert "value" not in _field(body, "openai_api_key")          # never echoed
    # Empty string clears the override.
    client.post("/api/settings", json={"values": {"openai_api_key": ""}})
    assert "openai_api_key" not in cfg.read_overrides()


def test_reset_clears_all_overrides(client):
    client.post("/api/settings", json={"values": {"ocr_dpi": 300}})
    assert cfg.read_overrides()
    r = client.post("/api/settings/reset")
    assert r.status_code == 200
    assert cfg.read_overrides() == {}
    assert _field(r.json(), "ocr_dpi")["value"] == 200          # back to default
