# Copilot Instructions

## Project overview

A proof-of-concept for an **MCP (Model Context Protocol) server** that exposes OSDU information resources — OpenAPI specs and schema definitions — to AI assistants. The current phase validates semantic search quality (embedding model, chunking strategy, retrieval relevance) before wrapping the retrieval logic as MCP tools. The two entry-point scripts are `osdu-index` and `osdu-query`.

## Setup and commands

```bash
# Install dependencies (uses uv, not pip)
uv sync

# Index specs and/or schemas (required before starting the MCP server)
osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db
osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db --reset  # drop + recreate collections

# Start the MCP server (stdio transport)
osdu-mcp --db ./chroma_db

# Interactive semantic query session (for manual testing / PoC validation)
osdu-query --db ./chroma_db
osdu-query --db ./chroma_db --top 8 --mode api     # api | schema | both
```

## Architecture

Three modules in `src/osdu_discovery/`:

- **`indexer.py`** — reads OpenAPI YAML/JSON files from `specs/` and OSDU schema JSON files from `schemas/`, extracts text representations of each API operation and schema property, and upserts them into two persisted ChromaDB collections.
- **`query.py`** — interactive CLI for manual testing of semantic search quality. Also contains `query_collection()`, which is the shared retrieval function used by the MCP server.
- **`mcp_server.py`** — MCP server (FastMCP, stdio transport). Loads ChromaDB collections once at startup, then exposes a single `search_osdu(query, mode, top_k)` tool. Reuses `query_collection()` from `query.py`.

### Entry points

| Command | Module | Purpose |
|---|---|---|
| `osdu-index` | `indexer:main` | Build/update the ChromaDB index |
| `osdu-query` | `query:main` | Interactive CLI for manual search testing |
| `osdu-mcp` | `mcp_server:main` | MCP server (stdio) for AI assistant integration |

### ChromaDB collections

| Collection | Content | ID format |
|---|---|---|
| `osdu_api_operations` | One entry per API operation | `{service}__{operationId}` |
| `osdu_schema_properties` | One entry per schema kind + one per property | `kind__{kind}` / `prop__{kind}__{dotted_path}` |

Both collections use `hnsw:space: cosine`. Scores < 0.3 = strong, 0.3–0.5 = good, 0.5–0.7 = weak, > 0.7 = poor.

The persisted index lives in `chroma_db/` and only needs to be regenerated when files in `specs/` or `schemas/` change.

## Key conventions

- **Package manager is `uv`** — use `uv sync`, `uv run`, and `uv add`. Do not use `pip install` directly.
- **OSDU schema kind strings** are derived from `schemaInfo.schemaIdentity` (authority:source:entityType:major.minor.patch). Filename is a fallback if the identity block is absent.
- **OSDU schemas store domain data under the `data` key** — `flatten_schema_properties` and `format_kind_summary` always drill into `schema.properties.data` before extracting properties.
- **`allOf` merging is one level deep** — deeper chains are only partially resolved. Cross-file `$ref` pointers are replaced with `[external ref: ...]` text rather than resolved.
- **ChromaDB document IDs are capped at 500 characters** (ChromaDB limit is 512) — property IDs with long dotted paths are truncated.
- **Dual-collection query interleaves results by rank** (api[0], schema[0], api[1], …) so neither collection dominates when `--mode both` is active.
- **Indexing is idempotent via `upsert`** — re-running without `--reset` updates changed entries and adds new ones without duplicating unchanged ones.
- **`rich`** is used for all console output (progress spinners, panels, tables, rules). Avoid `print()` directly; use the module-level `console = Console()` instance.

## Data layout

```
specs/      # OpenAPI YAML and JSON files (one per OSDU service)
schemas/    # OSDU schema JSON files (one per kind/version)
chroma_db/  # Persisted ChromaDB vector store (gitignored, generated)
src/osdu_discovery/
    indexer.py     # entry point: osdu-index
    query.py       # entry point: osdu-query  (also exports query_collection())
    mcp_server.py  # entry point: osdu-mcp
claude_desktop_config.example.json  # Claude Desktop wiring example
```

## MCP server conventions

- Collections are loaded **once at startup** — `_api_col` and `_schema_col` are module-level globals in `mcp_server.py`. Do not reload per request.
- The server **fails fast** at startup if `chroma_db/` is missing or contains no collections, printing to stderr and exiting with code 1.
- All output from the `search_osdu` tool is **plain Markdown** — no `rich` console markup (rich is for the interactive CLI only).
- When adding new tools, follow the same pattern: accept simple scalar args, return a plain Markdown string.
- The `query_collection()` function in `query.py` is the **shared retrieval primitive** — import it rather than duplicating the ChromaDB call logic.

## Client config files

Example configs for wiring the MCP server into each supported client are included in the repo root. Replace `/path/to/osdu-mcp-poc` with the absolute path in each.

| File | Client |
|---|---|
| `claude_desktop_config.example.json` | Claude Desktop — merge `mcpServers` block into `~/Library/Application Support/Claude/claude_desktop_config.json` |
| `copilot_mcp_config.example.json` | GitHub Copilot CLI — merge into `~/.copilot/mcp-config.json`, or use `/mcp add` in the interactive session |
| `cursor_mcp.example.json` | Cursor — merge `mcpServers` block into `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) |
| `vscode_mcp.example.json` | VS Code — merge into `.vscode/mcp.json`. Note VS Code uses the `servers` key and requires `"type": "stdio"`, so the `mcpServers` examples are **not** interchangeable with it |
| `opencode.example.json` | OpenCode — copy to `opencode.json` in project root, or merge `mcp` block into `~/.config/opencode/opencode.json` |

When adding a new client example, keep the server name `osdu-discovery`: clients namespace tools as `<server-name>-<tool-name>`, and a name containing a space or other character outside `[A-Za-z0-9_-]` yields an invalid tool name that some clients drop silently. Keep `command` as an absolute path to `uv` — clients spawn the server without the user's shell `PATH`.

