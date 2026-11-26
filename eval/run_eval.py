"""Compare retrieval methods on eval/questions.yaml.

    uv run python eval/run_eval.py                  # all available methods
    uv run python eval/run_eval.py --methods bm25   # just one

Methods
  dense    the former search_osdu backend: ChromaDB + all-MiniLM-L6-v2
           (needs `uv sync --group eval` and eval/dense_index.py first)
  bm25     today's search_osdu: keyword search (BM25, camelCase-aware)
  hybrid   dense and bm25 merged by reciprocal-rank fusion
  catalog  an LLM picks from a compact catalog, as an agent would with a
           list_kinds / list_operations tool (needs the `claude` CLI logged in)

Every method searches the same corpus (eval/corpus.py), so differences come
from the method. Metrics per question type: hit@1, hit@5 and MRR@10, scored
both on the raw result list and with repeated (kind, path) results from
older schema versions collapsed ("dedup"), since the index holds every version.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import corpus as corpus_mod  # noqa: E402
from osdu_discovery.search import BM25 as SearchBM25  # noqa: E402

HERE = Path(__file__).parent
ROOT = HERE.parent
K = 10


# ------------------------------------------------------------------ helpers


def kind_name(kind: str) -> str:
    """osdu:wks:master-data--Well:1.5.0 -> master-data--Well"""
    return re.sub(r":\d+\.\d+\.\d+$", "", kind).removeprefix("osdu:wks:")


def kind_version(kind: str) -> tuple[int, ...]:
    m = re.search(r":(\d+)\.(\d+)\.(\d+)$", kind)
    return tuple(map(int, m.groups())) if m else ()


def result_key(doc: dict, qtype: str) -> str:
    if qtype == "api":
        return f"{doc['method']} {doc['path']}"
    if qtype == "property":
        return f"{kind_name(doc['kind'])} {doc['path']}"
    return kind_name(doc["kind"])


def doc_id(doc: dict) -> str:
    """Full identity of a retrieved document, before any version collapsing."""
    if "service" in doc:
        return f"{doc['service']} {doc['method']} {doc['path']}"
    return f"{doc['kind']} {doc.get('path', '')}"


def is_gold(key: str, gold: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(key, g) for g in gold)


def score(keys: list[str], gold: list[str]) -> int | None:
    """1-based rank of the first gold hit in the top K, or None."""
    for i, k in enumerate(keys[:K], 1):
        if is_gold(k, gold):
            return i
    return None


def dedup(keys: list[str]) -> list[str]:
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ------------------------------------------------------------------ methods


class Dense:
    name = "dense"

    def __init__(self, db: Path = ROOT / "chroma_db"):
        import chromadb

        client = chromadb.PersistentClient(path=str(db))
        self.api = client.get_collection("osdu_api_operations")
        self.schema = client.get_collection("osdu_schema_properties")

    def search(self, q: str, qtype: str) -> list[dict]:
        col = self.api if qtype == "api" else self.schema
        r = col.query(query_texts=[q], n_results=K * 3)
        return r["metadatas"][0]


class BM25:
    """The keyword search behind `search_osdu` (osdu_discovery.search.BM25),
    one index per collection."""

    name = "bm25"

    def __init__(self, corpus: dict):
        self.idx = {"api": SearchBM25(corpus["api"]), "schema": SearchBM25(corpus["schema"])}

    def search(self, q: str, qtype: str) -> list[dict]:
        hits = self.idx["api" if qtype == "api" else "schema"].search(q, K * 3)
        return [doc for doc, _ in hits]


class Hybrid:
    """Reciprocal-rank fusion of dense and BM25: the usual cheap fix when
    embeddings miss exact identifiers and keywords miss paraphrases."""

    name = "hybrid"

    def __init__(self, dense: Dense, bm25: BM25, k: int = 60):
        self.dense, self.bm25, self.k = dense, bm25, k

    def search(self, q: str, qtype: str) -> list[dict]:
        # Fuse on the full document identity (service-qualified operation,
        # versioned kind and property), so one retriever returning several
        # versions of the same answer can't out-vote a document both rank
        # first. Collapsing versions is left to scoring.
        fused: dict[str, float] = defaultdict(float)
        docs: dict[str, dict] = {}
        for results in (self.dense.search(q, qtype), self.bm25.search(q, qtype)):
            for rank, d in enumerate(results, 1):
                key = doc_id(d)
                fused[key] += 1 / (self.k + rank)
                docs.setdefault(key, d)
        return [docs[k] for k in sorted(fused, key=fused.get, reverse=True)]


class Catalog:
    """An LLM picks from compact catalogs, the way an agent would use a
    list tool and then a describe tool. Uses the `claude` CLI headless, with
    no tools and no project context, so it only sees the catalog text."""

    name = "catalog"

    def __init__(self, corpus: dict, model: str = "haiku"):
        self.model = model
        self.api = corpus["api"]
        latest: dict[str, dict] = {}
        for d in corpus["schema"]:
            if d["type"] != "kind":
                continue
            name = kind_name(d["kind"])
            if name not in latest or kind_version(d["kind"]) > kind_version(latest[name]["kind"]):
                latest[name] = d
        self.kinds = latest
        self.props: dict[str, list[dict]] = defaultdict(list)
        for d in corpus["schema"]:
            if d["type"] == "property" and latest.get(kind_name(d["kind"]), {}).get("kind") == d["kind"]:
                self.props[kind_name(d["kind"])].append(d)
        self.api_catalog = "\n".join(
            f"{i}. [{o['service']}] {o['method']} {o['path']} — {o['summary'][:90]}"
            for i, o in enumerate(self.api)
        )
        self.kind_catalog = "\n".join(
            f"{name} — {self._first_sentence(d['text'])}" for name, d in sorted(latest.items())
        )
        self.calls = 0

    @staticmethod
    def _first_sentence(text: str) -> str:
        m = re.search(r"Description: (.+)", text)
        desc = m.group(1) if m else ""
        return desc.split(". ")[0][:110]

    def _ask(self, prompt: str) -> list[str]:
        self.calls += 1
        out = subprocess.run(
            [
                "claude", "-p", "--model", self.model, "--tools", "", "--strict-mcp-config",
                "--no-session-persistence", "--setting-sources", "", "--output-format", "json",
                "--system-prompt",
                "You pick items from catalogs. Reply with a JSON array of strings only, no prose.",
            ],
            input=prompt, capture_output=True, text=True, timeout=300, cwd=tempfile.gettempdir(),
        )
        res = json.loads(out.stdout)
        if res.get("is_error"):
            raise RuntimeError(res.get("result"))
        m = re.search(r"\[.*\]", res["result"], re.S)
        return json.loads(m.group(0)) if m else []

    def search(self, q: str, qtype: str) -> list[dict]:
        if qtype == "api":
            picks = self._ask(
                f"OSDU API operations, one per line as 'N. [service] METHOD path — summary':\n\n"
                f"{self.api_catalog}\n\nQuestion: {q}\n\n"
                "Return the line numbers N of the 5 operations that best answer the question, "
                'best first, as a JSON array of strings, e.g. ["12", "40"].'
            )
            return [self.api[int(p)] for p in picks if str(p).isdigit() and int(p) < len(self.api)]

        kinds = self._ask(
            f"OSDU record kinds, one per line as 'kind — description':\n\n{self.kind_catalog}\n\n"
            f"Question: {q}\n\nReturn the {5 if qtype == 'kind' else 3} kinds most likely to "
            "answer it, best first, as a JSON array of the exact kind strings."
        )
        kinds = [k for k in kinds if k in self.kinds]
        if qtype == "kind":
            return [self.kinds[k] for k in kinds]
        listing = []
        for k in kinds:
            for d in self.props[k]:
                m = re.search(r"Description: (.+)", d["text"])
                listing.append(f"{k} {d['path']} — {(m.group(1) if m else '')[:80]}")
        paths = self._ask(
            "OSDU schema properties, one per line as 'kind path — description':\n\n"
            + "\n".join(listing)
            + f"\n\nQuestion: {q}\n\nReturn the 5 properties that best answer it, best first, "
            "as a JSON array of strings in the form 'kind path' (exactly as listed, without the description)."
        )
        by_key = {f"{kind_name(d['kind'])} {d['path']}": d for k in kinds for d in self.props[k]}
        return [by_key[p] for p in paths if p in by_key]


# ------------------------------------------------------------------ runner


def check_gold(questions: list[dict], corpus: dict) -> list[str]:
    keys = {
        "api": {result_key(d, "api") for d in corpus["api"]},
        "property": {result_key(d, "property") for d in corpus["schema"] if d["type"] == "property"},
        "kind": {result_key(d, "kind") for d in corpus["schema"]},
    }
    return [
        f"{q['id']}: {g}"
        for q in questions
        for g in q["gold"]
        if not any(fnmatch.fnmatchcase(k, g) for k in keys[q["type"]])
    ]


def evaluate(method, questions: list[dict], workers: int = 1) -> list[dict]:
    def one(q: dict) -> dict:
        t0 = time.time()
        try:
            docs = method.search(q["q"], q["type"])
            error = None
        except Exception as exc:
            docs, error = [], str(exc)[:200]
        keys = [result_key(d, q["type"]) for d in docs]
        return {
            "id": q["id"], "type": q["type"], "vocab": q["vocab"],
            "rank": score(keys, q["gold"]), "rank_dedup": score(dedup(keys), q["gold"]),
            "top3": dedup(keys)[:3], "seconds": round(time.time() - t0, 3), "error": error,
        }

    if workers <= 1:
        return [one(q) for q in questions]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, questions))


def summarise(rows: list[dict], key: str) -> dict:
    n = len(rows)
    ranks = [r[key] for r in rows]
    return {
        "n": n,
        "hit@1": sum(1 for r in ranks if r == 1) / n,
        "hit@5": sum(1 for r in ranks if r and r <= 5) / n,
        "mrr": sum(1 / r for r in ranks if r) / n,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="dense,bm25,hybrid,catalog")
    ap.add_argument("--model", default="haiku", help="model for the catalog method")
    ap.add_argument("--workers", type=int, default=6, help="parallel questions for the catalog method")
    ap.add_argument("--out", default=str(HERE / "results.json"))
    args = ap.parse_args()

    questions = yaml.safe_load((HERE / "questions.yaml").read_text())["questions"]
    # Rebuilt every run (about 3 s) so it always matches specs/, schemas/
    # and the extraction code; the cache is only for interactive use.
    corpus = corpus_mod.build()
    bad = check_gold(questions, corpus)
    if bad:
        print("Gold answers not found in the corpus:\n  " + "\n  ".join(bad))
        return 1

    # Keep results of methods not re-run this time (the catalog run is slow).
    out = Path(args.out)
    results = json.loads(out.read_text()) if out.exists() else {}
    cache: dict[str, object] = {}

    def get(name):
        if name not in cache:
            cache[name] = Dense() if name == "dense" else BM25(corpus)
        return cache[name]

    for name in args.methods.split(","):
        t0 = time.time()
        if name in ("dense", "bm25"):
            method = get(name)
        elif name == "hybrid":
            method = Hybrid(get("dense"), get("bm25"))
        elif name == "catalog":
            method = Catalog(corpus, model=args.model)
        else:
            raise SystemExit(f"unknown method {name}")
        setup = time.time() - t0
        rows = evaluate(method, questions, args.workers if name == "catalog" else 1)
        errors = [r for r in rows if r["error"]]
        if errors:
            # An operational failure (no CLI, expired login, timeout) is not a
            # retrieval miss: don't score it, and don't overwrite results.
            print(f"\n{name}: {len(errors)} of {len(rows)} questions failed; aborting, {out} unchanged.")
            print("  first error:", errors[0]["error"])
            return 1
        results[name] = {"setup_seconds": round(setup, 1), "rows": rows}
        print(f"\n== {name}  (setup {setup:.1f}s{', ' + str(len(errors)) + ' errors' if errors else ''})")
        print(f"{'type':10} {'n':>3} {'hit@1':>6} {'hit@5':>6} {'mrr':>6} | dedup: {'hit@1':>6} {'hit@5':>6} {'mrr':>6}")
        for t in ("api", "property", "kind", "all"):
            sub = [r for r in rows if t == "all" or r["type"] == t]
            a, d = summarise(sub, "rank"), summarise(sub, "rank_dedup")
            print(
                f"{t:10} {a['n']:>3} {a['hit@1']:>6.0%} {a['hit@5']:>6.0%} {a['mrr']:>6.2f} |"
                f"        {d['hit@1']:>6.0%} {d['hit@5']:>6.0%} {d['mrr']:>6.2f}"
            )

    out.write_text(json.dumps(results, indent=1))
    print(f"\nPer-question results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
