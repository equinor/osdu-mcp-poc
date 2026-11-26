"""Tests for the schema documents the indexer builds (src/osdu_discovery/indexer.py),
on the shipped schemas. These are the documents `search_osdu` embeds and the
eval searches."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from osdu_discovery.indexer import (
    SchemaResolver,
    extract_kind_string,
    extract_schema_entries,
    extract_service_name,
    flatten_schema_properties,
    load_file,
)

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def entries(rel_path: str) -> dict[str, dict]:
    """Property path -> index entry, for one schema file."""
    path = str(SCHEMAS / rel_path)
    doc = load_file(path)
    out = extract_schema_entries(doc, extract_kind_string(doc, os.path.basename(path)), SchemaResolver(), path)
    return {e["metadata"]["path"]: e for e in out if e["metadata"]["type"] == "property"}


@pytest.fixture(scope="module")
def welllog() -> dict[str, dict]:
    return entries("work-product-component/WellLog.1.5.0.json")


def test_units_from_frame_of_reference(welllog):
    assert "Unit of measure: UOM:length" in welllog["data.TopMeasuredDepth"]["text"]


def test_relationship_on_array_items(welllog):
    assert "Relates to: reference-data/osduregion" in welllog["data.ResourceHostRegionIDs"]["text"]


def test_group_only_relationship(welllog):
    assert "Relates to: dataset/*" in welllog["data.Artefacts[].ResourceID"]["text"]


def test_oneof_branches_are_indexed_and_marked():
    coring = entries("master-data/Coring.1.1.0.json")
    text = coring["data.ConventionalCoring.CoreTypeID"]["text"]
    assert "Relates to: reference-data/conventionalcoretype" in text
    assert "Present in only some records" in text
    assert "data.SidewallCoring.CoreTypeID" in coring
    assert "Present in only some records" not in coring["data.GeoContexts"]["text"]


def test_content_kind_paths_have_no_data_prefix():
    nav = entries("content/SeismicTraceNavigation.1.0.0.json")
    assert "Name" in nav and not any(p.startswith("data.") for p in nav)
    assert "Property path: Name\n" in nav["Name"]["text"]


def test_ids_unchanged_for_data_wrapped_kinds(welllog):
    assert welllog["data.WellboreID"]["id"] == (
        "prop__osdu:wks:work-product-component--WellLog:1.5.0__WellboreID"
    )


def test_paths_are_unique_per_kind():
    path = str(SCHEMAS / "master-data/Wellbore.1.5.1.json")
    doc = load_file(path)
    resolved = SchemaResolver().resolve(doc, base_dir=os.path.dirname(path), root=doc)
    paths = [p["path"] for p in flatten_schema_properties(resolved["properties"]["data"], "data")]
    assert len(paths) == len(set(paths))


def test_generic_spec_title_falls_back_to_filename():
    assert extract_service_name({"info": {"title": "OpenAPI definition"}}, "Geospatial.yaml") == "Geospatial"
    assert extract_service_name({"info": {"title": "Storage Service"}}, "Storage.yaml") == "Storage Service"


def test_geojson_geometry_variants_are_not_expanded():
    wellbore = entries("master-data/Wellbore.1.5.1.json")
    assert "data.SpatialLocation.Wgs84Coordinates.features[].geometry" in wellbore
    assert not any(".geometry." in p for p in wellbore)
    assert "data.GeoContexts[].FieldID" in wellbore
