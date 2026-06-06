"""Task 0.1 — core types round-trip and enforce enums."""

import pytest
from pydantic import ValidationError

from app.engines.fie.models import Citation, EvidenceItem, FactRef


def test_factref_roundtrip():
    f = FactRef(
        company="MTL", metric="revenue", label="Revenue", year=2024,
        value=91_534_501.0, statement="pl", level="headline", sheet="P&L", cell="F6",
    )
    dumped = f.model_dump()
    restored = FactRef.model_validate(dumped)
    assert restored == f
    assert restored.unit == "Rupees in thousand"
    assert restored.provenance_basis == "none"


def test_factref_allows_none_metric_and_value():
    f = FactRef(
        company="MTL", metric=None, label="Tractors", year=2024, value=None,
        statement="pl", level="detail", sheet="PL1 - Revenue", cell="E5",
    )
    assert f.metric is None and f.value is None


def test_enum_enforced():
    with pytest.raises(ValidationError):
        FactRef(
            company="MTL", metric="revenue", label="x", year=2024, value=1.0,
            statement="cashflow", level="headline", sheet="P&L", cell="F6",  # bad statement
        )


def test_evidence_and_citation_roundtrip():
    c = Citation(ref_id="C1", kind="financial", display="AR 2025, p108",
                 locator={"page": 108})
    e = EvidenceItem(claim="rev=91.5bn", value=91_534_501.0, kind="statement",
                     citations=[c])
    assert EvidenceItem.model_validate(e.model_dump()) == e
