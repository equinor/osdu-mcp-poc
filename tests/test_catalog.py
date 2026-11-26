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
    # The server's kind list matches the catalog's: abstract/ schemas aren't kinds.
    assert len(mcp_server._catalog.kinds) == len(catalog.kinds)
    assert "AbstractCommonResources" not in mcp_server.list_kinds()
    assert mcp_server.get_schema("AbstractCommonResources", resolved=False).startswith("# Schema:")
    # The "group--Name" form list_kinds prints must work in get_schema too.
    assert mcp_server.get_schema("master-data--Wellbore", resolved=False).startswith(
        "# Schema: master-data--Wellbore\n"
    )


# ------------------------------------------------ fixes from the PR 12 review


def test_generic_spec_title_falls_back_to_filename(catalog):
    assert "## Geospatial" in catalog.list_operations(service="geospatial")
    assert "OpenAPI definition" not in catalog.list_operations()


def test_bad_spec_file_is_skipped(tmp_path):
    (tmp_path / "Broken.yaml").write_text("paths: [unclosed\n")
    (tmp_path / "Ok.yaml").write_text(
        "info: {title: Ok Service}\npaths:\n  /ping:\n    get: {summary: Ping}\n"
    )
    c = Catalog()
    assert c.load_specs(str(tmp_path)) == 1
    assert c.spec_errors and c.spec_errors[0].startswith("Broken.yaml")


def test_kinds_without_a_group(catalog):
    assert "## (no group)" in catalog.list_kinds()
    assert "- Manifest" in catalog.list_kinds(group="(no group)")
    assert catalog.list_properties("Manifest").startswith("# Properties of osdu:wks:Manifest:1.0.0")
    assert catalog.find_kind("osdu:wks:DataCollection:1.0.0").name == "DataCollection"


def test_content_kinds_have_no_data_prefix(catalog):
    text = catalog.list_properties("content--SeismicTraceNavigation")
    assert "\n- Name (string)" in text and "data.Name" not in text


def test_contains_matches_the_displayed_path(catalog):
    assert "- data.WellboreID (string)" in catalog.list_properties("WellLog", contains="data.WellboreID")


def test_oneof_branches_are_listed_and_marked(catalog):
    coring = catalog.list_properties("Coring", contains="CoreTypeID")
    assert "data.ConventionalCoring.CoreTypeID" in coring
    assert "data.SidewallCoring.CoreTypeID" in coring and "one of]" in coring
    geo = catalog.list_properties("Wellbore", contains="GeoContexts[].FieldID")
    assert "→ master-data--Field; one of" in geo


def test_relationships_on_array_items_and_group_only(catalog):
    region = catalog.list_properties("WellLog", contains="ResourceHostRegionIDs")
    assert "→ reference-data--OSDURegion" in region
    artefact = catalog.list_properties("WellLog", contains="Artefacts[].ResourceID")
    assert "→ any dataset record" in artefact


def test_frame_of_reference_units(catalog):
    assert "unit: UOM:length" in catalog.list_properties("WellLog", contains="data.TopMeasuredDepth")
