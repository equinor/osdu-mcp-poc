"""Keyword search (BM25) over OSDU API operations and schema properties.

Backs `search_osdu` and `osdu-query`. The index is built in memory from the
spec and schema folders when the server starts, in a few seconds, so there is
no index to build or store. It replaces an embedding index (ChromaDB with
all-MiniLM-L6-v2) that took about 26 minutes to build and 1.7 GB on disk.
The evaluation in eval/README.md found keyword search level with or ahead of
it on every question type.

The documents are the same ones the embedding index held: one per API
operation, one per kind, and one per schema property of every schema version.
Results for older versions of the same kind and property are collapsed, so
the top results are distinct answers.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from osdu_discovery.indexer import (
    SchemaResolver,
    extract_api_operations,
    extract_kind_string,
    extract_schema_entries,
    extract_service_name,
    is_spec_file,
    load_file,
)

# ----------------------------------------------------------------- documents


def build_documents(specs: str | None = None, schemas: str | None = None) -> dict:
    """{"api": [...], "schema": [...], "errors": [...]}; each document is
    {"id", "text", **metadata}. Files that fail to load are skipped and
    listed in "errors"."""
    api: list[dict] = []
    schema: list[dict] = []
    errors: list[str] = []
    if specs:
        for fn in sorted(os.listdir(specs)):
            if not is_spec_file(fn):
                continue
            try:
                spec = load_file(os.path.join(specs, fn))
                ops = extract_api_operations(spec, extract_service_name(spec, fn))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{fn}: {exc}")
                continue
            api += [{"id": op["id"], "text": op["text"], **op["metadata"]} for op in ops]
    if schemas:
        resolver = SchemaResolver()
        for dirpath, dirnames, filenames in os.walk(schemas):
            if os.path.basename(dirpath) == "abstract":
                dirnames[:] = []  # inlined into every kind that $refs them
                continue
            dirnames.sort()
            for fn in sorted(filenames):
                if not is_spec_file(fn):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    doc = load_file(path)
                    kind = extract_kind_string(doc, fn)
                    for e in extract_schema_entries(doc, kind, resolver, path):
                        schema.append({"id": e["id"], "text": e["text"], **e["metadata"]})
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{fn}: {exc}")
    return {"api": api, "schema": schema, "errors": errors}


# --------------------------------------------------------------------- BM25

# camelCase-aware: "WellboreMarkerSet" -> wellbore, marker, set; "OSDURegion" -> osdu, region
_TOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP = set(
    "a an the of to for in on at by is are was be it its this that with from as "
    "and or how do does i my me we what which where when who can into one all "
    "get".split()
)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text) if t.lower() not in _STOP]


@dataclass
class BM25:
    docs: list[dict]
    k1: float = 1.5
    b: float = 0.75
    _postings: dict[str, list[tuple[int, int]]] = field(default_factory=dict, repr=False)
    _lengths: list[int] = field(default_factory=list, repr=False)
    _avg: float = 1.0

    def __post_init__(self) -> None:
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, d in enumerate(self.docs):
            tf = Counter(tokens(d["text"]))
            self._lengths.append(sum(tf.values()))
            for t, n in tf.items():
                postings[t].append((i, n))
        self._postings = dict(postings)
        self._avg = sum(self._lengths) / max(len(self._lengths), 1)

    def search(self, query: str, k: int) -> list[tuple[dict, float]]:
        n = len(self.docs)
        scores: dict[int, float] = defaultdict(float)
        for t in set(tokens(query)):
            plist = self._postings.get(t)
            if not plist:
                continue
            idf = math.log(1 + (n - len(plist) + 0.5) / (len(plist) + 0.5))
            for i, tf in plist:
                denom = tf + self.k1 * (1 - self.b + self.b * self._lengths[i] / self._avg)
                scores[i] += idf * tf * (self.k1 + 1) / denom
        best = sorted(scores, key=scores.get, reverse=True)[:k]
        return [(self.docs[i], scores[i]) for i in best]


# ------------------------------------------------------------------- search

_VERSION = re.compile(r":\d+\.\d+\.\d+$")

# How many raw results to read per wanted result before collapsing versions.
OVERSAMPLE = 6


def answer_key(doc: dict) -> str:
    """The answer a document stands for, ignoring schema version."""
    if "method" in doc:
        return f"{doc['service']} {doc['method']} {doc['path']}"
    return f"{_VERSION.sub('', doc['kind'])} {doc.get('path', '')}"


@dataclass
class SearchIndex:
    api: BM25 | None = None
    schema: BM25 | None = None
    errors: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, specs: str | None = None, schemas: str | None = None) -> "SearchIndex":
        docs = build_documents(specs, schemas)
        return cls(
            api=BM25(docs["api"]) if docs["api"] else None,
            schema=BM25(docs["schema"]) if docs["schema"] else None,
            errors=docs["errors"],
        )

    def search(self, query: str, source: str, top_k: int) -> list[dict]:
        """Top distinct answers from one collection ("api" or "schema").
        Each hit: {"source", "id", "text", "score", "metadata", "versions"},
        where "versions" counts the schema versions collapsed into it."""
        index = self.api if source == "api" else self.schema
        if index is None:
            return []
        hits: dict[str, dict] = {}
        for doc, score in index.search(query, top_k * OVERSAMPLE):
            key = answer_key(doc)
            if key in hits:
                hits[key]["versions"] += 1
                continue
            if len(hits) == top_k:
                continue  # keep counting versions of the answers already kept
            meta = {k: v for k, v in doc.items() if k not in ("id", "text")}
            hits[key] = {
                "source": source, "id": doc["id"], "text": doc["text"],
                "score": score, "metadata": meta, "versions": 1,
            }
        return list(hits.values())
