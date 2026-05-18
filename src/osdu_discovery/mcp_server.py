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
import sys
from itertools import zip_longest

import chromadb
from mcp.server.fastmcp import FastMCP

from osdu_discovery.query import query_collection

mcp = FastMCP("osdu-discovery")

# Collections loaded once at startup, shared across all tool calls.
_api_col: chromadb.Collection | None = None
_schema_col: chromadb.Collection | None = None


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


def main() -> None:
    parser = argparse.ArgumentParser(description="OSDU Discovery MCP server (stdio)")
    parser.add_argument(
        "--db",
        default="./chroma_db",
        help="Path to ChromaDB persistence folder (default: ./chroma_db)",
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

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
