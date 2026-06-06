"""Shared test config: make ``app`` importable and locate output workbooks."""

import os
import sys

import pytest

# Disable per-IP rate limiting under test (the suite fires many requests from one
# client IP); set before any app import so get_settings() picks it up. The limiter
# itself is unit-tested directly in tests/test_security.py.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
# don't write trace JSON files for every API-test request
os.environ.setdefault("FIE_TRACE_ENABLED", "false")

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_OUTPUTS = os.path.join(_BACKEND, "storage", "outputs")


@pytest.fixture(scope="session")
def outputs_dir() -> str:
    return _OUTPUTS


@pytest.fixture(scope="session")
def millat_path(outputs_dir) -> str:
    return os.path.join(outputs_dir, "millat_filled_fixed.xlsx")


@pytest.fixture(scope="session")
def lucky_path(outputs_dir) -> str:
    return os.path.join(outputs_dir, "lucky_filled_fixed.xlsx")


@pytest.fixture(scope="session")
def millat_store(millat_path):
    from app.engines.fie import FinancialFactStore
    return FinancialFactStore.from_workbook(millat_path)
