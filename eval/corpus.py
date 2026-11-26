"""Build the same documents osdu-index embeds, without embedding them.

Every retriever in the comparison searches this corpus, so differences in
results come from the retrieval method, not from different source text.
Cached as JSON next to this file (corpus_cache.json, git-ignored).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from osdu_discovery.indexer import (
    SchemaResolver,
    extract_api_operations,
    extract_kind_string,
    extract_schema_entries,
    extract_service_name,
    is_spec_file,
    load_file,
)

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(__file__).with_name("corpus_cache.json")


def build(specs: Path = ROOT / "specs", schemas: Path = ROOT / "schemas") -> dict:
    api = []
    for fn in sorted(os.listdir(specs)):
        if not is_spec_file(fn):
            continue
        spec = load_file(str(specs / fn))
        service = extract_service_name(spec, fn)
        for op in extract_api_operations(spec, service):
            api.append({"id": op["id"], "text": op["text"], **op["metadata"]})

    resolver = SchemaResolver()
    schema = []
    errors = []
    for dirpath, dirnames, filenames in os.walk(schemas):
        if os.path.basename(dirpath) == "abstract":
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            if not is_spec_file(fn):
                continue
            path = os.path.join(dirpath, fn)
            try:
                doc = load_file(path)
                kind = extract_kind_string(doc, fn)
                for e in extract_schema_entries(doc, kind, resolver, path):
                    schema.append({"id": e["id"], "text": e["text"], **e["metadata"]})
            except Exception as exc:  # same tolerance as the indexer
                errors.append(f"{fn}: {exc}")
    return {"api": api, "schema": schema, "errors": errors}


def load(rebuild: bool = False) -> dict:
    if CACHE.exists() and not rebuild:
        return json.loads(CACHE.read_text())
    corpus = build()
    CACHE.write_text(json.dumps(corpus))
    return corpus


if __name__ == "__main__":
    c = load(rebuild="--rebuild" in sys.argv)
    print(f"api: {len(c['api'])}  schema: {len(c['schema'])}  errors: {len(c['errors'])}")
