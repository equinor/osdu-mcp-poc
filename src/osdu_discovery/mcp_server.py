"""
OSDU Discovery — MCP Server

Exposes OSDU semantic search as an MCP tool for use with AI assistants
(Claude Desktop, VS Code Copilot, etc.) via stdio transport.

Requires a pre-built ChromaDB index. Run the indexer first:
    osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db

Start the server:
    osdu-mcp --db ./chroma_db
    uv run -m osdu_discovery.mcp_server --db ./chroma_db
"""

import argparse
import json
import os
import re
import sys
from itertools import zip_longest

import chromadb
from mcp.server.fastmcp import FastMCP

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
                kind = load_file(path).get("x-osdu-schema-source", "")
            except Exception:
                kind = ""
            if kind:
                _schema_index[kind.lower()] = path
            m = re.match(r"^(.*)\.(\d+)\.(\d+)\.(\d+)$", stem)
            if m:
                bare = m.group(1).lower()
                ver = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
                if bare not in versions or ver > versions[bare][0]:
                    versions[bare] = (ver, path)
            files += 1
    for bare, (_, path) in versions.items():
        _schema_index.setdefault(bare, path)
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
            "Run `osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db` first."
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


def main() -> None:
    parser = argparse.ArgumentParser(description="OSDU Discovery MCP server (stdio)")
    parser.add_argument(
        "--db",
        default="./chroma_db",
        help="Path to ChromaDB persistence folder (default: ./chroma_db)",
    )
    parser.add_argument(
        "--schemas",
        default=None,
        help="Path to the OSDU schema folder. Enables the `get_schema` tool.",
    )
    args = parser.parse_args()

    global _api_col, _schema_col

    try:
        client = chromadb.PersistentClient(path=args.db)
    except Exception as e:
        print(f"ERROR: Could not open ChromaDB at '{args.db}': {e}", file=sys.stderr)
        sys.exit(1)

    existing = {c.name for c in client.list_collections()}

    if "osdu_api_operations" in existing:
        _api_col = client.get_collection("osdu_api_operations")
    else:
        print("WARNING: 'osdu_api_operations' collection not found — API search unavailable.", file=sys.stderr)

    if "osdu_schema_properties" in existing:
        _schema_col = client.get_collection("osdu_schema_properties")
    else:
        print("WARNING: 'osdu_schema_properties' collection not found — schema search unavailable.", file=sys.stderr)

    if not _api_col and not _schema_col:
        print(
            f"ERROR: No collections found in '{args.db}'. "
            "Run osdu-index first.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.schemas:
        if os.path.isdir(args.schemas):
            n = _build_schema_index(args.schemas)
            print(
                f"Schema index: {n} files from '{args.schemas}' — get_schema enabled.",
                file=sys.stderr,
            )
        else:
            print(
                f"WARNING: --schemas path '{args.schemas}' not found — "
                "get_schema unavailable.",
                file=sys.stderr,
            )

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
