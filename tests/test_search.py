"""Tests for the keyword search behind search_osdu (src/osdu_discovery/search.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from osdu_discovery import mcp_server
from osdu_discovery.search import BM25, SearchIndex, answer_key, tokens

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def index() -> SearchIndex:
    return SearchIndex.build(str(ROOT / "specs"), str(ROOT / "schemas"))


def test_tokens_split_camel_case_and_drop_stop_words():
    assert tokens("WellboreMarkerSet") == ["wellbore", "marker", "set"]
    assert tokens("OSDURegion of the data") == ["osdu", "region", "data"]
    assert tokens("legaltags:validate v2") == ["legaltags", "validate", "v", "2"]


def test_bm25_ranks_the_matching_document_first():
    docs = [{"text": "wellbore trajectory azimuth"}, {"text": "file checksum size"}]
    hits = BM25(docs).search("azimuth", 5)
    assert [d["text"] for d, _ in hits] == ["wellbore trajectory azimuth"]


def test_builds_every_document(index):
    assert len(index.api.docs) == 366
    assert len(index.schema.docs) > 100_000
    assert not index.errors


def test_finds_exact_names(index):
    api = index.search("validate legal tags", "api", 5)
    assert api[0]["metadata"]["path"] == "/legaltags:validate"
    schema = index.search("curve mnemonic", "schema", 5)
    assert any(h["metadata"]["path"] == "data.Curves[].Mnemonic" for h in schema)


def test_older_versions_are_collapsed(index):
    hits = index.search("curve mnemonic GR RHOB", "schema", 10)
    keys = [answer_key({**h["metadata"]}) for h in hits]
    assert len(keys) == len(set(keys))
    assert any(h["versions"] > 1 for h in hits)


def test_search_osdu_tool(index, monkeypatch):
    monkeypatch.setattr(mcp_server, "_search", index)
    monkeypatch.setattr(mcp_server, "_search_started", True)
    mcp_server._search_ready.set()
    text = mcp_server.search_osdu("validate legal tags", mode="api", top_k=3)
    assert "**Endpoint:** POST /legaltags:validate" in text
    assert text.count("## Result ") == 3
    assert "Invalid mode" in mcp_server.search_osdu("x", mode="nope")


def test_search_osdu_without_folders(monkeypatch):
    monkeypatch.setattr(mcp_server, "_search_started", False)
    assert "--specs or --schemas" in mcp_server.search_osdu("anything")
