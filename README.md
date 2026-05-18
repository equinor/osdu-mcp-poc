# OSDU Discovery PoC

Semantic search over OSDU OpenAPI specs and schema files.
Validates indexing quality before building a full MCP server.

## Setup

```bash
uv sync
```

ChromaDB will download a local embedding model on first run (~90MB, once only).

## Folder layout

```
osdu-mcp-poc/
├── src/
│   └── osdu_discovery/
│       ├── __init__.py
│       ├── indexer.py
│       └── query.py
├── pyproject.toml
└── README.md
```

## Index

```bash
# Index both specs and schemas
osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db

# Specs only
osdu-index --specs ./specs --db ./chroma_db

# Schemas only
osdu-index --schemas ./schemas --db ./chroma_db

# Re-index from scratch (drop existing collections first)
osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db --reset
```

The index is persisted to `./chroma_db` and survives between runs.
You only need to re-index when specs/schemas change.

## Query

```bash
# Query both collections (default)
osdu-query --db ./chroma_db

# Show more results
osdu-query --db ./chroma_db --top 8

# API operations only
osdu-query --db ./chroma_db --mode api

# Schema properties only
osdu-query --db ./chroma_db --mode schema
```

### Example queries to try

```
find seismic surveys within a spatial polygon
get wireline log curves for a wellbore
retrieve measured depth of wellbore markers
search for wells by basin and spud date
download a dataset file
check entitlements for a user
register a new schema kind
get the trajectory stations for a directional well
```

Inside the query session, type `stats` to see index counts or `quit` to exit.

## Interpreting results

| Relevance label | Distance range | Meaning                        |
|-----------------|----------------|--------------------------------|
| strong          | < 0.30         | Very likely what you need      |
| good            | 0.30 – 0.50    | Probably relevant, worth checking |
| weak            | 0.50 – 0.70    | Tangentially related           |
| poor            | > 0.70         | Likely noise                   |

Distance is cosine distance (0 = identical meaning, 2 = opposite).

## Known limitations

- Cross-file `$ref` pointers in specs are not resolved (noted as `[external ref: ...]`).
  If your specs use `$ref` to separate schema files, consider bundling them first
  with a tool like [Redocly CLI](https://redocly.com/docs/cli/commands/bundle/):
  `redocly bundle openapi.yaml -o bundled.yaml`

- OSDU schema files with deeply nested `allOf` chains may not fully resolve.
  The indexer handles one level of `allOf` merging; deeper chains are left partial.

- ChromaDB's default embedding model (all-MiniLM-L6-v2) is fast and good enough
  for a PoC. For production, consider switching to a larger model or an API-based
  embedder for better semantic accuracy on domain-specific OSDU terminology.

## MCP server

The MCP server exposes OSDU semantic search as a tool for AI assistants (Claude Desktop, VS Code Copilot, etc.) via stdio transport.

**Prerequisite:** build the index first (see above).

```bash
osdu-mcp --db ./chroma_db
```

### Tool: `search_osdu`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Natural language description, e.g. *"get wireline log curves for a wellbore"* |
| `mode` | string | `"both"` | `"api"`, `"schema"`, or `"both"` |
| `top_k` | integer | `5` | Results returned per collection |

### Client configuration

Replace `/path/to/osdu-mcp-poc` with the absolute path to this repo in all examples below.

#### Claude Desktop

Merge the `mcpServers` block into `~/Library/Application Support/Claude/claude_desktop_config.json`.
See `claude_desktop_config.example.json` for the full file.

```json
{
  "mcpServers": {
    "osdu-discovery": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--project", "/path/to/osdu-mcp-poc", "osdu-mcp", "--db", "/path/to/osdu-mcp-poc/chroma_db"]
    }
  }
}
```

#### GitHub Copilot CLI

Run `/mcp add` inside the Copilot CLI interactive session and fill in the fields, **or** edit `~/.copilot/mcp-config.json` directly.
See `copilot_mcp_config.example.json` for the full file. The JSON format is identical to Claude Desktop.

#### OpenCode

Copy `opencode.example.json` to `opencode.json` in the project root (or merge into `~/.config/opencode/opencode.json` for global use):

```json
{
  "mcp": {
    "osdu-discovery": {
      "type": "local",
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--project", "/path/to/osdu-mcp-poc", "osdu-mcp", "--db", "/path/to/osdu-mcp-poc/chroma_db"],
      "enabled": true
    }
  }
}
```

## Next steps

1. Add a `get_spec` tool that returns the full OpenAPI spec or schema document for a result
2. Add a `find_workflow` tool for stitching multi-service paths
