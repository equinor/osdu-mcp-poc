# Retrieval evaluation

Does the embedding index behind `search_osdu` earn its 28-minute build and
1.4 GB footprint? This folder measures it against cheaper alternatives on
38 realistic questions.

```bash
uv run python eval/run_eval.py                         # all methods
uv run python eval/run_eval.py --methods bm25,hybrid   # a subset
```

- `questions.yaml` holds the questions and their gold answers: 18 API operations,
  13 schema properties and 7 record kinds. It uses mostly paraphrased wording,
  and every gold answer is checked to exist before a run.
- `corpus.py` rebuilds the exact documents `osdu-index` embeds in about 3 s,
  without embedding them, so every method searches the same text.
- `run_eval.py` runs the methods and prints hit@1, hit@5 and MRR@10 per
  question type. Per-question ranks go to `results.json`.

## Methods

| Method | What it is | Build | Query |
|---|---|---|---|
| `dense` | Today's `search_osdu`: ChromaDB with all-MiniLM-L6-v2 | ~28 min, 1.4 GB | ~70 ms |
| `bm25` | Keyword search that splits camelCase names, over the same documents | ~2 s, in memory | ~1 ms |
| `hybrid` | `dense` and `bm25` merged by reciprocal-rank fusion | the index | ~60 ms |
| `catalog` | An LLM picks from a compact list of operations or kinds, then from that kind's properties, as an agent would with list and describe tools | none | an LLM call or two |

## Results (2026-11-25, data-definitions v0.30.1, specs from `main`)

Scored with repeated results from older versions of the same kind collapsed
("dedup" in the script's output). That is what an agent effectively sees.

| Method | API hit@1 | API hit@5 | Property hit@1 | Property hit@5 | Kind hit@1 | Kind hit@5 | **All hit@1** | **All hit@5** | All MRR |
|---|---|---|---|---|---|---|---|---|---|
| `dense` (current) | 33% | 61% | 23% | 38% | 14% | 71% | **26%** | **55%** | 0.39 |
| `bm25` | 44% | 61% | 38% | 54% | 29% | 71% | **39%** | **61%** | 0.48 |
| `hybrid` | 28% | 61% | 46% | 62% | 57% | 71% | **39%** | **63%** | 0.50 |
| `catalog` (Haiku) | 100% | 100% | 62% | 69% | 71% | 86% | **82%** | **87%** | 0.84 |

Without dedup, `dense` drops to 45% hit@5 overall, because the index holds every
version of every kind and older versions crowd out other results.

### What this shows

- **Letting the model choose from a catalog wins by a wide margin.** It
  scores 82% hit@1 against 26% for today's search, and 100% on API questions.
  This uses Haiku, the smallest current model. It gets all 10 of the
  questions every search method missed into its top 5, such as "history of edits" →
  `GET /records/versions/{id}`, and "monthly oil and gas volumes" →
  `ProductionValues`.
- **Its 7 non-first answers:**
  - Two are second-ranked: kelly bushing and directional survey.
  - Three come from reference data. Its first step picked a code list instead
    of the record that holds the value:
    - `LogCurveType` instead of `WellLog` for the curve mnemonic and unit;
    - `AzimuthReferenceType` instead of `WellboreTrajectory`.

    These are easy wins for the searches, which all get them.
  - Two are debatable: `Sample` for core samples, and `WellActivity` for the
    current operator.

  Reference data makes up 594 of the 828 kinds, and about 70% of the kind
  catalog.
- **The embedding index is the weakest method, not the strongest.** Keyword
  search with no index at all ties or beats it on every question type. It
  builds in 2 seconds instead of 28 minutes.
- **The hybrid helps for schema questions** (property hit@1 46% vs 23%, kind
  hit@1 57% vs 14%) but not for API questions. It still needs the index.
- **All three methods miss the same questions.** 10 of the 38 are missed by
  every method in the top 5, for example:
  - "see the history of edits to a record" (`GET /records/versions/{id}`);
  - "be notified when records change" (`POST /subscription`);
  - "monthly oil and gas volumes" (`ProductionValues`);
  - "depth of a formation top" (`Markers[].MarkerMeasuredDepth`).

  These questions need a mapping from domain words ("history", "notified",
  "formation top") to OSDU names ("versions", "subscription", "marker"). That
  takes language understanding, which only the `catalog` method has.
- **Noise in today's results:** `dense` often ranks reference-data kinds such as
  `WellLogMethod`, `CementStageType` and `CalculationMethodType` at the top of
  schema questions.

### What the catalog costs

The catalog method needs no index, but the agent reads the catalog into its
context every time it uses it.

| Catalog | Size |
|---|---|
| API operations (366, with summaries) | ~8,700 tokens |
| Kinds (828, with first-sentence descriptions) | ~25,000 tokens |
| Kinds without reference data (234) | ~7,400 tokens |
| Properties of 3 chosen kinds | usually 1,000–4,000 tokens |

In this run each lookup took about 30 s, but that is mostly the overhead of
starting the `claude` CLI. Inside an MCP client the agent's own model does
the picking, so a lookup is just one more tool call and its output.

### What I'd build from this

1. **Add `list_operations` and `list_kinds` tools** that return the compact
   catalogs, and let the agent pick, then call `get_schema` or `find_path`.
   List reference data separately, or behind a filter, so the kind list stays
   around 7,000 tokens and the model isn't steered towards code lists.
   *Done:* `list_operations`, `list_kinds` (reference data hidden by default)
   and `list_properties`, in `src/osdu_discovery/catalog.py`. Their lines are
   grouped by service or group, so the format differs slightly from the
   numbered lists this run measured.
2. **Replace the embedding index with keyword search** for `search_osdu`, as a
   cheap way to look up exact names. It's level with or better than the index,
   builds in seconds, and removes the 1.4 GB, 28-minute build. That would also
   make the server practical for cloud agents.
3. **Re-run this evaluation with new questions** after building 1 and 2. The
   reference-data fix was found by studying these misses, so measuring it on
   the same 38 questions would overstate it.

### Caveats

- 38 questions is small: one question moves a type's score by 5–14 points
  and the overall score by about 2.6. Treat differences of one or two questions
  as noise. The main finding holds on every question type: `bm25` is level with
  or ahead of `dense` on hit@1 and hit@5 for API, property and kind questions.
- The questions were written by the same person who chose the gold answers.
  They are paraphrased on purpose, which favours methods that understand
  meaning over exact keywords, so this setup if anything favours `dense`.
- The gold set was widened once, after the first keyword-search run and before
  any other method ran, where the schema offers several correct answers. See
  the header of `questions.yaml`.
- The `catalog` method uses a model, and the search methods don't, so it isn't
  a like-for-like test of retrieval. The fair question is: which tool design
  gets an agent to the right answer? An agent using `search_osdu` also has a
  model, but it only sees the 5–10 results the search returns.
- The `catalog` run is one sample. Model choices vary a little between runs,
  so expect a few points of movement.
- An agent rarely stops at one search. It can re-query, or follow up with
  `get_schema`, which recovers some misses. This measures single-shot
  retrieval only.

## Running the catalog method

It calls the `claude` CLI headless, with no tools, MCP servers or settings
loaded, so the model sees only the catalog text. It sends the public OSDU
catalog to Anthropic's API under your Claude login. Sign in first:

```bash
claude auth login
uv run python eval/run_eval.py --methods catalog --model haiku
```

`--model sonnet` shows how much model quality matters. A run makes about 50
model calls: one per API or kind question, two per property question.
