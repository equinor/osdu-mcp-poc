"""Regression tests for the relationship graph (src/osdu_discovery/graph.py).

The synthetic tests pin the search rules on graphs of a few edges. The model
tests pin the same behaviour on the shipped LinkML model, using the cases
that first exposed each bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osdu_discovery.graph import Edge, OsduGraph

MODEL = Path(__file__).resolve().parents[1] / "model" / "osdu_latest.yaml"


def synthetic(*edges: Edge) -> OsduGraph:
    g = OsduGraph(classes={})
    for e in edges:
        for key in (e.source, e.target):
            if not OsduGraph.is_wildcard(key):
                g.entities.setdefault(key, "1.0.0")
        g.out_edges[e.source].append(e)
        g.in_edges[e.target].append(e)
    return g


def hops(path) -> list[tuple[str, str, str]]:
    return [(e.source, e.path, e.target) for _, e, _ in path]


# ------------------------------------------------------------ synthetic rules


def test_final_common_hop_after_a_domain_hop():
    # A -> B is a domain link; B -> C is inherited from a common mixin. The
    # common link may land on the goal, so A -> B -> C is a valid route.
    g = synthetic(
        Edge("master-data/A", "data.BID", "master-data/B"),
        Edge("master-data/B", "data.GeoContexts[].CID", "master-data/C", common=True, repeated=True),
    )
    paths = g.find_paths("master-data/A", "master-data/C")
    assert [hops(p) for p in paths] == [
        [
            ("master-data/A", "data.BID", "master-data/B"),
            ("master-data/B", "data.GeoContexts[].CID", "master-data/C"),
        ]
    ]


def test_common_hop_is_still_not_a_stepping_stone():
    g = synthetic(
        Edge("master-data/A", "data.GeoContexts[].BID", "master-data/B", common=True, repeated=True),
        Edge("master-data/B", "data.CID", "master-data/C"),
    )
    assert g.find_paths("master-data/A", "master-data/C") == []


def test_budget_grows_past_a_rejected_shortest_route():
    # S and G share a single-valued polymorphic slot on X (2 hops, rejected).
    # The real route S -> P -> Q -> R -> G is 4 hops, within max_hops=4.
    g = synthetic(
        Edge("master-data/X", "data.EntityID", "master-data/S"),
        Edge("master-data/X", "data.EntityID", "master-data/G"),
        Edge("master-data/S", "data.PID", "master-data/P"),
        Edge("master-data/P", "data.QID", "master-data/Q"),
        Edge("master-data/Q", "data.RID", "master-data/R"),
        Edge("master-data/R", "data.GID", "master-data/G"),
    )
    paths = g.find_paths("master-data/S", "master-data/G", max_hops=4)
    assert [len(p) for p in paths] == [4]


def test_shared_array_slot_links_its_entries():
    g = synthetic(
        Edge("master-data/Inventory", "data.Items[].EquipmentID", "master-data/Anchor", repeated=True),
        Edge("master-data/Inventory", "data.Items[].EquipmentID", "master-data/Pump", repeated=True),
    )
    paths = g.find_paths("master-data/Anchor", "master-data/Pump")
    assert len(paths) == 1 and len(paths[0]) == 2


def test_shared_scalar_slot_does_not_link():
    g = synthetic(
        Edge("master-data/FlowTest", "data.ReportingEntityID", "master-data/Wellbore"),
        Edge("master-data/FlowTest", "data.ReportingEntityID", "master-data/Reservoir"),
    )
    assert g.find_paths("master-data/Wellbore", "master-data/Reservoir") == []


def test_empty_entity_name_is_a_group_wildcard():
    g = OsduGraph(classes={})
    attr = {"annotations": {"relationship_targets": "dataset::, reference-data::, abstract::AbstractFoo"}}
    assert g._targets(attr) == ["dataset/*", "reference-data/*"]


# ------------------------------------------------------------- shipped model


@pytest.fixture(scope="module")
def graph() -> OsduGraph:
    if not MODEL.exists():
        pytest.skip(f"model not found: {MODEL}")
    return OsduGraph.load(str(MODEL))


def paths_between(g: OsduGraph, a: str, b: str, **kw):
    (start,) = g.resolve(a)
    (goal,) = g.resolve(b)
    return g.find_paths(start, goal, **kw)


def test_model_welllog_to_field_via_wellbore(graph):
    routes = [hops(p) for p in paths_between(graph, "WellLog", "Field")]
    assert [
        ("work-product-component/WellLog", "data.WellboreID", "master-data/Wellbore"),
        ("master-data/Wellbore", "data.GeoContexts[].FieldID", "master-data/Field"),
    ] in routes


def test_model_rig_equipment_through_inventory(graph):
    routes = [hops(p) for p in paths_between(graph, "RigAnchor", "RigPump")]
    slot = ("master-data/EquipmentInventory", "data.EquipmentInventory[].EquipmentID")
    assert [slot + ("master-data/RigAnchor",), slot + ("master-data/RigPump",)] in routes


def test_model_artefacts_resource_id(graph):
    paths = {e.path: e.target for e in graph.out_edges["work-product-component/WellLog"]}
    assert paths.get("data.Artefacts[].ResourceID") == "dataset/*"


def test_model_canonical_routes_unchanged(graph):
    best = hops(paths_between(graph, "WellLog", "Well")[0])
    assert best == [
        ("work-product-component/WellLog", "data.WellboreID", "master-data/Wellbore"),
        ("master-data/Wellbore", "data.WellID", "master-data/Well"),
    ]
    assert paths_between(graph, "File.Generic", "Wellbore")
