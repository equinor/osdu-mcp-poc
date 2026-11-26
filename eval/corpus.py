"""The documents every eval method searches: the ones `search_osdu` holds.

Built by osdu_discovery.search.build_documents, so the eval measures the same
text the server searches, and differences in results come from the retrieval
method. `load()` caches them as JSON next to this file (corpus_cache.json,
git-ignored) for interactive use; run_eval.py always rebuilds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from osdu_discovery.search import build_documents

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(__file__).with_name("corpus_cache.json")


def build(specs: Path = ROOT / "specs", schemas: Path = ROOT / "schemas") -> dict:
    return build_documents(str(specs), str(schemas))


def load(rebuild: bool = False) -> dict:
    if CACHE.exists() and not rebuild:
        return json.loads(CACHE.read_text())
    corpus = build()
    CACHE.write_text(json.dumps(corpus))
    return corpus


if __name__ == "__main__":
    c = load(rebuild="--rebuild" in sys.argv)
    print(f"api: {len(c['api'])}  schema: {len(c['schema'])}  errors: {len(c['errors'])}")
