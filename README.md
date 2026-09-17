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
Without it, `get_schema` is still listed but every call returns
`Schema lookup unavailable`, so only `search_osdu` does useful work.

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

Requires the server to be started with `--schemas`. Without it the tool is
still listed, but returns `Schema lookup unavailable`.

### Client configuration

This is a standard stdio MCP server, so it works with any MCP-capable client.
Clients differ in *where* the config file lives, *what the wrapper key is
called*, and — for OpenCode alone — *how the launch command is spelled*. The
executable and its arguments are the same everywhere; only the JSON around them
changes.

Two things to adjust in every example below:

1. **Replace `/path/to/osdu-mcp-poc`** with the absolute path to this repo.
   Relative paths and `~` are not expanded by most clients.
2. **Use the absolute path to your own `uv`.** Clients launch the server
   without your shell's `PATH`, so a bare `uv` usually fails with
   `ENOENT`/`command not found`. Find yours with `which uv` (macOS/Linux),
   `where uv` (Windows Command Prompt) or `where.exe uv` (PowerShell — plain
   `where` is an alias for `Where-Object` there). Common locations:

   | Platform | Typical path |
   |---|---|
   | macOS, Homebrew (Apple Silicon) | `/opt/homebrew/bin/uv` |
   | macOS, Homebrew (Intel) | `/usr/local/bin/uv` |
   | macOS/Linux, `uv` standalone installer | `/Users/<you>/.local/bin/uv`, `/home/<you>/.local/bin/uv` |
   | Windows | `C:\\Users\\<you>\\.local\\bin\\uv.exe` |

   On Windows, remember that JSON requires escaped backslashes (`\\`) or forward slashes.

**Keep the server name `osdu-discovery`.** Clients namespace tools as
`<server-name>-<tool-name>`, and names containing spaces or other characters
outside `[A-Za-z0-9_-]` produce an invalid tool name. Some clients drop the
tools *silently* while still showing the server as connected.

#### Which shape does my client use?

Almost every client uses one of three shapes. Pick the matching example file:

| Client | Config file | Wrapper key | Command shape | Example file |
|---|---|---|---|---|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)<br>`%APPDATA%\\Claude\\claude_desktop_config.json` (Windows) | `mcpServers` | `command` + `args` | `claude_desktop_config.example.json` |
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` | `mcpServers` | `command` + `args` | `copilot_mcp_config.example.json` |
| Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) | `mcpServers` | `command` + `args` | `cursor_mcp.example.json` |
| Windsurf, Zed, most others | client-specific | `mcpServers` | `command` + `args` | any `mcpServers` example |
| VS Code | `.vscode/mcp.json` (workspace) or **MCP: Open User Configuration** | **`servers`** | `command` + `args`, plus `type` | `vscode_mcp.example.json` |
| Claude Code | managed by CLI — see below | — | — | — |
| OpenCode | `opencode.json` (project) or `~/.config/opencode/opencode.json` | **`mcp`** | **single `command` array** | `opencode.example.json` |

Merge the block into the file if it already exists — don't overwrite it, as
these files usually hold other servers too.

#### `mcpServers` clients (Claude Desktop, Copilot CLI, Cursor, most others)

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

In the Copilot CLI you can also run `/mcp add` in an interactive session and
fill in the fields instead of editing the file.

#### VS Code

VS Code uses `servers`, not `mcpServers`, and wants an explicit `type` — the
block above will **not** work if pasted as-is. Use `vscode_mcp.example.json`,
or run **MCP: Add Server** from the Command Palette.

```json
{
  "servers": {
    "osdu-discovery": {
      "type": "stdio",
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--project", "/path/to/osdu-mcp-poc", "osdu-mcp", "--db", "/path/to/osdu-mcp-poc/chroma_db", "--schemas", "/path/to/osdu-mcp-poc/schemas"]
    }
  }
}
```

#### Claude Code

```bash
claude mcp add osdu-discovery -- /opt/homebrew/bin/uv run --project /path/to/osdu-mcp-poc osdu-mcp --db /path/to/osdu-mcp-poc/chroma_db --schemas /path/to/osdu-mcp-poc/schemas
```

Everything after `--` is the command to launch. Add `-s user` to make it
available outside the current project. Kept on one line so it pastes safely
into PowerShell as well as a POSIX shell.

#### OpenCode

OpenCode is the one client that does not take `command` + `args`. Its
`McpLocalConfig` wants a **single `command` array** holding the executable and
every argument, and the schema sets `additionalProperties: false`, so an `args`
key is rejected outright rather than ignored.

Copy `opencode.example.json` to `opencode.json` in the project root (or merge into `~/.config/opencode/opencode.json` for global use):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "osdu-discovery": {
      "type": "local",
      "command": ["/opt/homebrew/bin/uv", "run", "--project", "/path/to/osdu-mcp-poc", "osdu-mcp", "--db", "/path/to/osdu-mcp-poc/chroma_db", "--schemas", "/path/to/osdu-mcp-poc/schemas"],
      "enabled": true
    }
  }
}
```

Keeping `$schema` at the top is worth it: editors then flag a malformed entry
in place, instead of OpenCode rejecting the file at startup.

### Verifying the connection

Restart the client fully after editing its config — most read MCP config only
at startup.

To check the server independently of any client, run the same command from
your terminal:

```bash
/opt/homebrew/bin/uv run --project /path/to/osdu-mcp-poc osdu-mcp --db /path/to/osdu-mcp-poc/chroma_db --schemas /path/to/osdu-mcp-poc/schemas
```

A healthy start prints one line to stderr and then waits on stdin without
returning to the prompt:

```
Schema index: 1427 files from '/path/to/osdu-mcp-poc/schemas' — get_schema enabled.
```

If you passed `--schemas` and don't see that line, the path is wrong — the
server prints a `WARNING` and carries on with `get_schema` disabled. If the
command exits immediately instead of waiting, the error on stderr is the same
one the client is hitting.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Server fails to start, `ENOENT` / `command not found` | `command` is a bare `uv` or the wrong absolute path. Use the output of `which uv` / `where.exe uv`. |
| Exits immediately, `No collections found` (exit code 1) | The index has not been built, or `--db` points at the wrong folder. Run `osdu-index` first — see [Index](#index). `chroma_db/` is generated, not shipped in the repo. |
| Server connects but no tools appear | Server name contains a space or other unsupported character. Rename it to `osdu-discovery`. |
| `get_schema` returns `Schema lookup unavailable` | The server was started without `--schemas`, or the path given didn't exist. The tool is always registered, so this is a runtime message rather than a missing tool. |
| Tools missing after the client was already running | Config is only read at startup, and some clients cache the tool list. Restart the client. |

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
