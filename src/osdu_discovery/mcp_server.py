"""
OSDU Discovery — MCP Server

Exposes OSDU semantic search as an MCP tool for use with AI assistants
(Claude Desktop, VS Code Copilot, etc.) via stdio transport.

`search_osdu` needs a pre-built ChromaDB index. Run the indexer first:
    osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db

The list tools need only the spec and schema folders, so the server can also
run without an index (`--db` pointing at a missing folder just disables
`search_osdu`).

Start the server:
    osdu-mcp --db ./chroma_db --specs ./specs --schemas ./schemas
    uv run -m osdu_discovery.mcp_server --db ./chroma_db
"""

import argparse
import difflib
import json
import os
import re
import sys
from itertools import zip_longest

import chromadb
from mcp.server.fastmcp import FastMCP

from osdu_discovery.catalog import Catalog
from osdu_discovery.graph import OsduGraph
from osdu_discovery.indexer import SchemaResolver, load_file
from osdu_discovery.query import query_collection

mcp = FastMCP("osdu-discovery")

# Collections loaded once at startup, shared across all tool calls.
_api_col: chromadb.Collection | None = None
_schema_col: chromadb.Collection | None = None

# Schema-file lookup, populated at startup when --schemas is given.
# Maps lookup keys (full kind id, versioned stem, bare name) → file path.
_schema_index: dict[str, str] = {}
_resolver = SchemaResolver()

# Compact operation and kind lists for the list_* tools (--specs / --schemas).
_catalog = Catalog()

# Relationship graph from the LinkML Logical Data Model, loaded when --model is given.
_graph: OsduGraph | None = None


def _build_schema_index(schemas_dir: str) -> int:
    """Walk the schema folder and register every file under several lookup
    keys: the full kind id, the ``Name.major.minor.patch`` stem, and the bare
    ``Name`` (pointing at the highest version found)."""
    versions: dict[str, tuple[tuple[int, int, int], str]] = {}
    files = 0
    for dirpath, _, filenames in os.walk(schemas_dir):
        for fn in filenames:
            if not fn.endswith((".json", ".yaml", ".yml")):
                continue
            path = os.path.join(dirpath, fn)
            stem = re.sub(r"\.(json|ya?ml)$", "", fn, flags=re.IGNORECASE)
            _schema_index[stem.lower()] = path
            try:
                doc = load_file(path)
                kind = doc.get("x-osdu-schema-source", "")
            except Exception:
                kind = ""
            if kind:
                _schema_index[kind.lower()] = path
                _catalog.add_schema(path, doc)
            m = re.match(r"^(.*)\.(\d+)\.(\d+)\.(\d+)$", stem)
            if m:
                bare = m.group(1).lower()
                ver = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
                if bare not in versions or ver > versions[bare][0]:
                    versions[bare] = (ver, path)
            files += 1
    for bare, (_, path) in versions.items():
        _schema_index.setdefault(bare, path)
    # "group--Name" (the form list_kinds prints) -> highest version.
    for short, k in _catalog.kinds.items():
        _schema_index.setdefault(short, k.path)
    return files


def _format_hit(hit: dict, rank: int) -> str:
    meta = hit["metadata"]
    distance = hit["distance"]
    source = hit["source"]

    relevance = (
        "strong" if distance < 0.3
        else "good" if distance < 0.5
        else "weak" if distance < 0.7
        else "poor"
    )

    lines = [f"## Result {rank}  [{relevance}, distance={distance:.3f}]"]

    if source == "api":
        lines.append(f"**Type:** API operation")
        lines.append(f"**Service:** {meta.get('service', '')}")
        lines.append(f"**Endpoint:** {meta.get('method', '')} {meta.get('path', '')}")
        if meta.get("summary"):
            lines.append(f"**Summary:** {meta['summary']}")
    elif meta.get("type") == "kind":
        lines.append(f"**Type:** Schema kind")
        lines.append(f"**Kind:** {meta.get('kind', '')}")
    else:
        lines.append(f"**Type:** Schema property")
        lines.append(f"**Property path:** {meta.get('path', '')}")
        lines.append(f"**Kind:** {meta.get('kind', '')}")

    # Include the full indexed text so the assistant can read field details
    lines.append(f"\n**Indexed content:**\n```\n{hit['text']}\n```")
    return "\n".join(lines)


@mcp.tool()
def search_osdu(
    query: str,
    mode: str = "both",
    top_k: int = 5,
) -> str:
    """Search OSDU API operations and/or schema properties using natural language.

    Best for exact or near-exact names. For a question in domain words
    ("history of edits", "formation tops"), reading `list_operations` or
    `list_kinds` and picking is usually more reliable.

    Args:
        query: Natural language description of what you are looking for.
               Examples: "get wireline log curves for a wellbore",
               "register a new schema kind", "download a dataset file"
        mode: Which index to search. One of: "api" (OpenAPI operations only),
              "schema" (OSDU schema properties only), "both" (default).
        top_k: Number of results to return per collection (default 5).
    """
    if mode not in ("api", "schema", "both"):
        return f"Invalid mode '{mode}'. Use 'api', 'schema', or 'both'."

    if not _api_col and not _schema_col:
        return (
            "No ChromaDB collections loaded. "
            "Run `osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db` first, "
            "or use list_operations / list_kinds / list_properties instead."
        )

    if mode == "api" and not _api_col:
        return "Collection 'osdu_api_operations' not found. Run `osdu-index --specs ./specs --db ./chroma_db` first."
    if mode == "schema" and not _schema_col:
        return "Collection 'osdu_schema_properties' not found. Run `osdu-index --schemas ./schemas --db ./chroma_db` first."

    hits: list[dict] = []

    if mode == "both" and _api_col and _schema_col:
        api_hits = query_collection(_api_col, query, top_k, source_tag="api")
        schema_hits = query_collection(_schema_col, query, top_k, source_tag="schema")
        for pair in zip_longest(api_hits, schema_hits):
            hits.extend(h for h in pair if h is not None)
    elif mode == "api" or (mode == "both" and _api_col):
        hits = query_collection(_api_col, query, top_k, source_tag="api")
    elif mode == "schema" or (mode == "both" and _schema_col):
        hits = query_collection(_schema_col, query, top_k, source_tag="schema")

    if not hits:
        return "No results found."

    header = f"# OSDU search results for: {query}\nMode: {mode}  |  Top-K: {top_k}\n"
    result_blocks = [_format_hit(hit, i + 1) for i, hit in enumerate(hits)]
    return header + "\n\n---\n\n".join(result_blocks)


@mcp.tool()
def get_schema(kind: str, resolved: bool = True) -> str:
    """Return the full JSON Schema document for an OSDU kind.

    Use this after `search_osdu` to see a matched property in its full
    context — e.g. how `data.DatasetProperties.FileSourceInfo.FileSource`
    nests inside a `File.Generic` record, and which sibling attributes
    (`Checksum`, `FileSize`, `PreloadFilePath`) travel with it. This is the
    reliable way to answer "which attribute, in which schema, links metadata
    to file content".

    Args:
        kind: An OSDU kind. Accepts the full id
              ("osdu:wks:dataset--File.Generic:1.1.0"), the versioned name
              ("File.Generic.1.1.0"), or the bare name ("File.Generic" —
              resolves to the highest available version).
        resolved: When True (default), cross-file $refs into the abstract
              schemas are inlined so the data model is complete. When False,
              the raw on-disk schema is returned with $ref pointers intact.
    """
    if not _schema_index:
        return (
            "Schema lookup unavailable: the server was started without "
            "--schemas. Restart with `osdu-mcp --db ./chroma_db --schemas ./schemas`."
        )

    key = kind.strip().lower()
    path = _schema_index.get(key)

    if path is None:
        tokens = re.split(r"[:\-./]+", key)
        needle = next((t for t in reversed(tokens) if t), key)
        matches = sorted({k for k in _schema_index if needle and needle in k})
        if not matches:
            return f"No schema found for '{kind}'."
        listed = "\n".join(f"  - {m}" for m in matches[:25])
        more = f"\n  ... and {len(matches) - 25} more" if len(matches) > 25 else ""
        return f"No exact match for '{kind}'. Did you mean:\n{listed}{more}"

    try:
        doc = load_file(path)
    except Exception as exc:  # noqa: BLE001
        return f"Failed to load schema '{kind}' from {path}: {exc}"

    if resolved:
        doc = _resolver.resolve(
            doc, base_dir=os.path.dirname(os.path.abspath(path)), root=doc
        )

    text = json.dumps(doc, indent=2)
    max_chars = 80_000
    note = ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated — call with resolved=False for a smaller doc]"
        note = " (truncated)"

    mode = "fully resolved" if resolved else "raw, $refs intact"
    return (
        f"# Schema: {kind}{note}\n"
        f"Source file: {path}\nForm: {mode}\n\n```json\n{text}\n```"
    )


# ---------------------------------------------------------------------------
# Catalog tools (no index needed)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_operations(service: str = "", contains: str = "") -> str:
    """List every OSDU API operation as one short line: method, path, summary.

    Start here for "which endpoint do I call to ...?" questions. Read the
    list and pick, rather than searching: it maps domain wording ("history of
    edits", "be notified of changes") to the right endpoint far more
    reliably. The full list of about 370 operations is roughly 6,500 tokens.

    Args:
        service: Only operations of services whose name contains this text,
              e.g. "storage", "search", "wellbore". Empty (default) lists all.
        contains: Only operations whose path or summary contains this text.
    """
    if not _catalog.operations:
        return (
            "Operation list unavailable: the server was started without --specs. "
            "Restart with `osdu-mcp ... --specs ./specs`."
        )
    return _catalog.list_operations(service, contains)


@mcp.tool()
def list_kinds(group: str = "", contains: str = "", include_reference_data: bool = False) -> str:
    """List OSDU record kinds (latest version) with a one-line description each.

    Start here for "which record type holds ...?" questions, then call
    `list_properties` on the kinds you pick. Reference-data kinds (about 600
    code lists: units, CRS, type values) are hidden by default; they rarely
    hold the record you want, and the list stays near 7,500 tokens without them.

    Args:
        group: Only this group: "master-data", "work-product-component",
              "dataset", "reference-data", "content", "work-product", "type".
        contains: Only kinds whose name or description contains this text.
        include_reference_data: List reference-data kinds too when no group
              is given (about 25,000 tokens in all).
    """
    if not _catalog.kinds:
        return (
            "Kind list unavailable: the server was started without --schemas. "
            "Restart with `osdu-mcp ... --schemas ./schemas`."
        )
    return _catalog.list_kinds(group, contains, include_reference_data)


@mcp.tool()
def list_properties(kind: str, contains: str = "") -> str:
    """List every data property of an OSDU kind as one line: path, type,
    description, and the kinds it references.

    A compact alternative to `get_schema` (which returns the full JSON
    Schema, often 50-80 KB). Includes properties inherited from the abstract
    schemas, with nested arrays shown as `data.Curves[].Mnemonic`.

    Args:
        kind: Full kind id, "group--Name" (as `list_kinds` prints it), or
              bare name ("WellLog"). The latest version is used.
        contains: Only properties whose path or description contains this text.
    """
    if not _catalog.kinds:
        return (
            "Property list unavailable: the server was started without --schemas. "
            "Restart with `osdu-mcp ... --schemas ./schemas`."
        )
    return _catalog.list_properties(kind, contains)


# ---------------------------------------------------------------------------
# Relationship tools (LinkML Logical Data Model)
# ---------------------------------------------------------------------------

_NO_GRAPH = (
    "Relationship graph unavailable: the server was started without --model. "
    "Restart with `osdu-mcp ... --model ./model/osdu_latest.yaml`."
)


def _label(key: str) -> str:
    if key == "*":
        return "any record"
    if key.endswith("/*"):
        return f"any {key[:-2]} record"
    return _graph.kind_id(key) if _graph else key


def _short(key: str) -> str:
    """'master-data/Wellbore' -> 'master-data--Wellbore' (wildcards spelled out)."""
    if _graph and _graph.is_wildcard(key):
        return _label(key)
    return key.replace("/", "--", 1)


def _resolve_one(kind: str) -> tuple[str | None, str | None]:
    """(class key, error message)."""
    hits = _graph.resolve(kind)
    if len(hits) == 1:
        return hits[0], None
    if hits:
        listed = "\n".join(f"  - {_label(h)}" for h in hits)
        return None, f"'{kind}' is ambiguous. Use one of:\n{listed}"
    needle = re.split(r"[:/]+|--", kind.strip().lower())[-1]
    near = sorted(k for k in _graph.entities if needle and needle in k.lower())[:15]
    if not near:
        names = {k.partition("/")[2].lower(): k for k in _graph.entities}
        near = [names[n] for n in difflib.get_close_matches(needle, list(names), n=8, cutoff=0.75)]
    if near:
        listed = "\n".join(f"  - {_label(k)}" for k in near)
        return None, f"No entity '{kind}' in the logical model. Did you mean:\n{listed}"
    return None, f"No entity '{kind}' in the logical model."


def _flags(edge) -> str:
    flags = []
    if edge.deprecated:
        flags.append("DEPRECATED")
    if edge.common:
        flags.append("common")
    return f"  _({', '.join(flags)})_" if flags else ""


@mcp.tool()
def get_relationships(
    kind: str,
    direction: str = "both",
    include_reference_data: bool = False,
    include_common: bool = False,
) -> str:
    """List which records an OSDU kind references, and which kinds reference it.

    Built from the OSDU LinkML Logical Data Model, so it covers every
    relationship in the schema corpus, including ones inherited from abstract
    schemas and nested arrays, each with its JSON path in a record. Use it for
    "what links to a Wellbore?", "what does a WellLog point at?", or impact
    analysis before changing a kind. For multi-hop questions use `find_path`.

    Args:
        kind: Full kind id ("osdu:wks:master-data--Wellbore:1.5.1"),
              "master-data--Wellbore", or bare name ("Wellbore").
        direction: "out" (this kind's reference properties), "in" (kinds
              whose properties point at this one), or "both" (default).
        include_reference_data: Include links to/from reference-data kinds
              (unit, CRS, type lists...). Off by default: they are numerous.
        include_common: Include links every entity inherits from shared
              abstract schemas (NameAliases organisation, technical assurance,
              GeoContexts, vertical-measurement reference...). Off by default.
    """
    if not _graph:
        return _NO_GRAPH
    if direction not in ("out", "in", "both"):
        return "Invalid direction. Use 'out', 'in' or 'both'."
    key, err = _resolve_one(kind)
    if err:
        return err

    def keep(other: str, e) -> bool:
        if e.common and not include_common:
            return False
        return include_reference_data or not other.startswith("reference-data/")

    lines = [f"# Relationships of {_label(key)}"]
    hidden = 0
    if direction in ("out", "both"):
        by_path: dict[str, list] = {}
        for e in _graph.out_edges.get(key, []):
            if keep(e.target, e):
                by_path.setdefault(e.path, []).append(e)
            else:
                hidden += 1
        lines.append(f"\n## Outgoing — properties of this kind that reference other records ({len(by_path)})")
        for path, es in sorted(by_path.items()):
            targets = ", ".join(sorted({_label(e.target) for e in es}))
            lines.append(f"- `{path}` → {targets}{_flags(es[0])}")
    if direction in ("in", "both"):
        by_src: dict[str, list] = {}
        for e in _graph.incoming(key):
            if keep(e.source, e) and not _graph.is_wildcard(e.target):
                by_src.setdefault(e.source, []).append(e)
            elif not _graph.is_wildcard(e.target):
                hidden += 1
        lines.append(f"\n## Incoming — kinds whose properties reference this kind ({len(by_src)})")
        for src, es in sorted(by_src.items()):
            paths = ", ".join(f"`{p}`" for p in sorted({e.path for e in es}))
            lines.append(f"- {_label(src)}: {paths}{_flags(es[0])}")
        # Group-level wildcards ("any dataset" etc.) are specific enough to list.
        group_wild: dict[str, set[str]] = {}
        any_wild: set[str] = set()
        for e in _graph.incoming(key):
            if e.target == "*":
                any_wild.add(e.path)
            elif _graph.is_wildcard(e.target):
                group_wild.setdefault(f"{e.path} → {_label(e.target)}", set()).add(e.source)
        if group_wild:
            lines.append("\n## Incoming via generic properties (this kind is one allowed target)")
            for desc, sources in sorted(group_wild.items(), key=lambda x: -len(x[1])):
                path, _, tgt = desc.partition(" → ")
                eg = ", ".join(_short(s) for s in sorted(sources)[:4])
                more = f", ... ({len(sources)} kinds)" if len(sources) > 4 else ""
                lines.append(f"- `{path}` → {tgt}, held by: {eg}{more}")
        if any_wild:
            props = sorted(any_wild)
            shown = ", ".join(f"`{p}`" for p in props[:6]) + (", ..." if len(props) > 6 else "")
            lines.append(
                f"\n_Can also be the target of {len(props)} \"any record\" properties ({shown}) — omitted._"
            )
    if hidden:
        lines.append(
            f"\n_{hidden} reference-data or common links hidden; "
            "pass include_reference_data / include_common to show them._"
        )
    return "\n".join(lines)


@mcp.tool()
def find_path(
    from_kind: str,
    to_kind: str,
    max_hops: int = 4,
    limit: int = 8,
    include_reference_data: bool = False,
) -> str:
    """Find how two OSDU kinds are connected through record references.

    Answers "how do I get from a WellLog to its Well?", "which records tie a
    File to a Wellbore?", "how are WellboreMarkerSets related to
    SeismicHorizons?". Each hop names the JSON property holding the id, and
    its direction: `A.data.X → B` means an A record stores B's id in
    `data.X` (so from a B you find As by searching on `data.X`); hops may
    point either way along the path. Paths are
    returned best first: fewest hops, with links inherited from common
    abstract schemas ranked lower, plus alternatives up to one hop longer.

    Args:
        from_kind: Start kind (full id, "group--Name", or bare name).
        to_kind: Target kind.
        max_hops: Longest path considered (default 4).
        limit: Maximum number of paths returned (default 8).
        include_reference_data: Allow reference-data kinds as intermediate
              hops. Off by default (nearly everything shares a unit or CRS).
    """
    if not _graph:
        return _NO_GRAPH
    a, err = _resolve_one(from_kind)
    if err:
        return err
    b, err = _resolve_one(to_kind)
    if err:
        return err
    if a == b:
        return "Start and target are the same kind."
    paths = _graph.find_paths(
        a, b, max_hops=max(1, min(max_hops, 6)), limit=max(1, min(limit, 25)),
        include_reference_data=include_reference_data,
    )
    if not paths:
        return (
            f"No path from {_label(a)} to {_label(b)} within {max_hops} hops "
            "(reference-data hops, hub kinds such as Organisation, and generic "
            "'any record' links are not used as intermediate steps)."
        )
    lines = [f"# Paths from {_label(a)} to {_label(b)}"]
    for i, p in enumerate(paths, 1):
        lines.append(f"\n## Path {i} — {len(p)} hop{'s' if len(p) > 1 else ''}")
        node = a
        for other, e, _direction in p:
            target = _short(e.target)
            if _graph.is_wildcard(e.target):
                covered = node if e.source == other else other
                target += f" (here {_short(covered)})"
            lines.append(f"- {_short(e.source)} `{e.path}` → {target}{_flags(e)}")
            node = other
    return "\n".join(lines)


@mcp.tool()
def get_deprecations(kind: str) -> str:
    """List deprecated properties of an OSDU kind and their replacements.

    Check this before writing or querying records: OSDU marks many
    properties deprecated (e.g. FacilityNameAliases → NameAliases) and the
    LinkML model records the successor where one is named.

    Args:
        kind: Full kind id, "group--Name", or bare name.
    """
    if not _graph:
        return _NO_GRAPH
    key, err = _resolve_one(kind)
    if err:
        return err
    deps = _graph.deprecations.get(key, [])
    if not deps:
        return f"{_label(key)} has no deprecated properties."
    lines = [f"# Deprecated properties of {_label(key)} ({len(deps)})"]
    for d in sorted(deps, key=lambda d: d.path):
        repl = f" → use `{d.replacement}`" if d.replacement else " (no replacement named)"
        note = d.note if len(d.note) <= 200 else d.note[:197] + "..."
        lines.append(f"- `{d.path}`{repl}\n  {note}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="OSDU Discovery MCP server (stdio)")
    parser.add_argument(
        "--db",
        default="./chroma_db",
        help="Path to ChromaDB persistence folder (default: ./chroma_db)",
    )
    parser.add_argument(
        "--specs",
        default=None,
        help="Path to the OpenAPI spec folder. Enables the `list_operations` tool.",
    )
    parser.add_argument(
        "--schemas",
        default=None,
        help="Path to the OSDU schema folder. Enables get_schema, list_kinds and list_properties.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Path to the LinkML osdu_latest.yaml. Enables get_relationships, find_path and get_deprecations.",
    )
    args = parser.parse_args()

    global _api_col, _schema_col, _graph

    existing: set[str] = set()
    client = None
    if os.path.isdir(args.db):
        try:
            client = chromadb.PersistentClient(path=args.db)
            existing = {c.name for c in client.list_collections()}
        except Exception as e:
            print(f"WARNING: Could not open ChromaDB at '{args.db}': {e}", file=sys.stderr)
    else:
        print(f"WARNING: no ChromaDB folder at '{args.db}' — search_osdu unavailable.", file=sys.stderr)

    if "osdu_api_operations" in existing:
        _api_col = client.get_collection("osdu_api_operations")
    elif client:
        print("WARNING: 'osdu_api_operations' collection not found — API search unavailable.", file=sys.stderr)

    if "osdu_schema_properties" in existing:
        _schema_col = client.get_collection("osdu_schema_properties")
    elif client:
        print("WARNING: 'osdu_schema_properties' collection not found — schema search unavailable.", file=sys.stderr)

    if not (_api_col or _schema_col or args.specs or args.schemas or args.model):
        print(
            f"ERROR: No collections found in '{args.db}' and no --specs, --schemas or "
            "--model given, so no tool would work. Run osdu-index first, or pass a folder.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.specs:
        if os.path.isdir(args.specs):
            n = _catalog.load_specs(args.specs)
            print(f"Operation list: {n} operations from '{args.specs}' — list_operations enabled.", file=sys.stderr)
        else:
            print(f"WARNING: --specs path '{args.specs}' not found — list_operations unavailable.", file=sys.stderr)

    if args.schemas:
        if os.path.isdir(args.schemas):
            n = _build_schema_index(args.schemas)
            print(
                f"Schema index: {n} files, {len(_catalog.kinds)} kinds from '{args.schemas}' — "
                "get_schema, list_kinds and list_properties enabled.",
                file=sys.stderr,
            )
        else:
            print(
                f"WARNING: --schemas path '{args.schemas}' not found — "
                "get_schema unavailable.",
                file=sys.stderr,
            )

    if args.model:
        try:
            _graph = OsduGraph.load(args.model)
            n_edges = sum(len(v) for v in _graph.out_edges.values())
            print(
                f"Relationship graph: {len(_graph.entities)} kinds, {n_edges} reference "
                f"links from '{args.model}' — relationship tools enabled.",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"WARNING: could not load --model '{args.model}': {exc} — "
                "relationship tools unavailable.",
                file=sys.stderr,
            )

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
