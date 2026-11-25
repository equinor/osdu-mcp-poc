"""Tests for the list_operations, list_kinds and list_properties catalogs
(src/osdu_discovery/catalog.py), on the shipped specs and schemas."""

from __future__ import annotations

from pathlib import Path

import pytest

from osdu_discovery import mcp_server
from osdu_discovery.catalog import Catalog, first_sentence

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    c = Catalog()
    c.load_specs(str(ROOT / "specs"))
    c.load_schemas(str(ROOT / "schemas"))
    return c


def test_first_sentence():
    assert first_sentence("A hole in the ground. More text.", 110) == "A hole in the ground"
    assert first_sentence("Type, e.g. 2D or 3D. More.", 110).startswith("Type, e.g. 2D")
    assert first_sentence("x" * 50, 10) == "x" * 9 + "…"


def test_operations_cover_the_eval_gold_answers(catalog):
    text = catalog.list_operations()
    for line in ("- GET /records/versions/{id}", "- POST /subscription", "- POST /query_with_cursor"):
        assert line in text


def test_operation_filters(catalog):
    storage = catalog.list_operations(service="storage")
    assert "## Storage Service" in storage and "## Search Service" not in storage
    assert "No service 'nope'" in catalog.list_operations(service="nope")
    assert "POST /legaltags:validate" in catalog.list_operations(contains="legaltags")


def test_kinds_hide_reference_data_by_default(catalog):
    text = catalog.list_kinds()
    assert "- master-data--Wellbore — A hole in the ground" in text
    assert "work-product-component--WellboreMarkerSet" in text
    assert "reference-data--" not in text.split("_")[0]
    assert "reference-data kinds" in text  # the hint about the hidden group
    assert "reference-data--AzimuthReferenceType" in catalog.list_kinds(group="reference-data")
    assert "reference-data--UnitOfMeasure" in catalog.list_kinds(include_reference_data=True)


def test_kinds_keep_latest_version_only(catalog):
    assert catalog.kinds["master-data--wellbore"].kind_id == "osdu:wks:master-data--Wellbore:1.5.1"


def test_find_kind_forms(catalog):
    for form in ("WellLog", "work-product-component--WellLog", "osdu:wks:work-product-component--WellLog:1.0.0"):
        assert catalog.find_kind(form).name == "WellLog"
    assert isinstance(catalog.find_kind("Trajectory"), list)


def test_properties_include_inherited_nested_and_links(catalog):
    text = catalog.list_properties("WellLog")
    assert "- data.Curves[].Mnemonic (string)" in text
    assert "data.WellboreID (string)" in text and "→ master-data--Wellbore" in text
    assert "data.ResourceSecurityClassification" in text  # from the abstract schemas
    operator = catalog.list_properties("Wellbore", contains="operator")
    assert "data.CurrentOperatorID" in operator and "data.Curves" not in operator


def test_server_tools_and_get_schema_accept_listed_names(catalog, monkeypatch):
    monkeypatch.setattr(mcp_server, "_catalog", Catalog())
    monkeypatch.setattr(mcp_server, "_schema_index", {})
    assert "--specs" in mcp_server.list_operations()
    assert "--schemas" in mcp_server.list_kinds()
    mcp_server._build_schema_index(str(ROOT / "schemas"))
    assert "master-data--Wellbore" in mcp_server.list_kinds()
    # The "group--Name" form list_kinds prints must work in get_schema too.
    assert mcp_server.get_schema("master-data--Wellbore", resolved=False).startswith(
        "# Schema: master-data--Wellbore\n"
    )
