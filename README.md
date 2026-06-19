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

## Schema resolution

OSDU entity schemas put their real payload inside `data`, which is an `allOf`
of cross-file `$ref`s into `../abstract/*.json`. The `schemas/` folder therefore
mirrors the OSDU `Generated/` layout (`abstract/`, `dataset/`, `master-data/`,
`reference-data/`, `work-product-component/`, …) and the indexer:

- walks it **recursively**;
- **resolves cross-file `$ref`s** (relative to each referencing file) and inlines
  the abstract schemas, with circular-reference guarding;
- merges nested `allOf` chains to whatever depth they reach.

This is what lets a query like *"which attribute links file metadata to its
content"* surface `data.DatasetProperties.FileSourceInfo.FileSource` — a property
that lives two cross-file `$ref` hops deep inside `AbstractFile` / `AbstractDataset`.
The `abstract/` schemas are not indexed as standalone kinds; they appear inlined
in every entity that references them.

### Schema provenance

`schemas/` is a point-in-time copy of the `Generated/` folder from the OSDU
[data-definitions](https://community.opengroup.org/osdu/data/data-definitions)
repository, tag **`v0.29.1`** (commit `be852720`, 2026-01-29). It does not track
upstream releases automatically — to refresh, replace `schemas/` with the
`Generated/` folder from a newer data-definitions tag, re-run `osdu-index`, and
update this note.

## Known limitations

- Cross-file `$ref` pointers **in the OpenAPI specs** are not resolved (noted as
  `[external ref: ...]`). If your specs use `$ref` to separate schema files,
  bundle them first with a tool like
  [Redocly CLI](https://redocly.com/docs/cli/commands/bundle/):
  `redocly bundle openapi.yaml -o bundled.yaml`. (Schema-file cross-refs *are*
  resolved — see above.)

- ChromaDB's default embedding model (all-MiniLM-L6-v2) is fast and good enough
  for a PoC. For production, consider switching to a larger model or an API-based
  embedder for better semantic accuracy on domain-specific OSDU terminology.

## MCP server

The MCP server exposes OSDU semantic search as a tool for AI assistants (Claude Desktop, VS Code Copilot, etc.) via stdio transport.

**Prerequisite:** build the index first (see above).

```bash
osdu-mcp --db ./chroma_db --schemas ./schemas
```

`--schemas` is optional but recommended: it enables the `get_schema` tool.
Without it, only `search_osdu` is available.

### Tool: `search_osdu`

Semantic search — finds *candidate* operations and properties for a need.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Natural language description, e.g. *"get wireline log curves for a wellbore"* |
| `mode` | string | `"both"` | `"api"`, `"schema"`, or `"both"` |
| `top_k` | integer | `5` | Results returned per collection |

### Tool: `get_schema`

Returns the **full** JSON Schema for a kind — the reliable way to confirm
*which attribute, in which schema* does something, and what sibling attributes
travel with it. Use it after `search_osdu` to see a matched property in context.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `kind` | string | required | Full id (`osdu:wks:dataset--File.Generic:1.1.0`), versioned name (`File.Generic.1.1.0`), or bare name (`File.Generic`, → highest version) |
| `resolved` | boolean | `true` | `true` inlines cross-file `$ref`s into the abstract schemas; `false` returns the raw on-disk doc |

Requires the server to be started with `--schemas`.

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
      "args": ["run", "--project", "/path/to/osdu-mcp-poc", "osdu-mcp", "--db", "/path/to/osdu-mcp-poc/chroma_db", "--schemas", "/path/to/osdu-mcp-poc/schemas"]
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
      "args": ["run", "--project", "/path/to/osdu-mcp-poc", "osdu-mcp", "--db", "/path/to/osdu-mcp-poc/chroma_db", "--schemas", "/path/to/osdu-mcp-poc/schemas"],
      "enabled": true
    }
  }
}
```

## Next steps

1. Add a `get_spec` tool that returns the full OpenAPI spec for an API result
   (the schema-side equivalent, `get_schema`, now exists)
2. Add a `find_workflow` tool for stitching multi-service paths — e.g. tying a
   `File.Generic` metadata record to the File-service operations that produce
   and consume its `FileSource`

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for
development setup, the pull-request process, and commit conventions.

## Security

To report a security vulnerability, follow the process in
[`SECURITY.md`](SECURITY.md). Do not open a public issue.

## License

Licensed under the [Apache License 2.0](LICENSE).
