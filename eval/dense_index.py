"""Build the ChromaDB embedding index the `dense` eval method searches.

`search_osdu` used this index (all-MiniLM-L6-v2 embeddings) until keyword
search replaced it. It is kept only so the comparison in eval/README.md can be
re-run. It embeds the same documents the keyword search holds, and takes
about 26 minutes and 1.7 GB.

    uv sync --group eval
    uv run python eval/dense_index.py              # -> ./chroma_db
    uv run python eval/dense_index.py --db /tmp/chroma_db
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from osdu_discovery.search import build_documents

ROOT = Path(__file__).resolve().parents[1]
COLLECTIONS = {"api": "osdu_api_operations", "schema": "osdu_schema_properties"}
BATCH = 1000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--specs", default=str(ROOT / "specs"))
    ap.add_argument("--schemas", default=str(ROOT / "schemas"))
    ap.add_argument("--db", default=str(ROOT / "chroma_db"))
    args = ap.parse_args()

    try:
        import chromadb
    except ImportError:
        print("chromadb is not installed. Run `uv sync --group eval` first.", file=sys.stderr)
        return 1

    docs = build_documents(args.specs, args.schemas)
    for err in docs["errors"]:
        print(f"skipped {err}", file=sys.stderr)
    client = chromadb.PersistentClient(path=args.db)
    for source, name in COLLECTIONS.items():
        try:
            client.delete_collection(name)  # always from scratch
        except Exception:  # noqa: BLE001 - not there yet
            pass
        col = client.create_collection(name, metadata={"hnsw:space": "cosine"})
        items = docs[source]
        t0 = time.time()
        for i in range(0, len(items), BATCH):
            batch = items[i : i + BATCH]
            col.add(
                ids=[d["id"] for d in batch],
                documents=[d["text"] for d in batch],
                metadatas=[{k: v for k, v in d.items() if k not in ("id", "text")} for d in batch],
            )
            print(f"\r{name}: {min(i + BATCH, len(items))}/{len(items)}  ({time.time() - t0:.0f}s)", end="", file=sys.stderr)
        print(file=sys.stderr)
    print(f"Done: {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
