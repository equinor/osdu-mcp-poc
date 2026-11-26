# OSDU Discovery MCP – getting started

OSDU Discovery is an MCP server that lets AI assistants (GitHub Copilot,
Claude and others) look things up in OSDU. It answers questions such as:

- which API call to use ("how do I see the edit history of a record?");
- which record type and property hold a value ("where is the depth of a
  formation top?");
- how records link to each other ("how do I get from a WellLog to its
  field?");
- which properties are deprecated, and what replaces them.

It uses local copies of the OSDU OpenAPI specs and JSON Schemas
(data-definitions v0.30.1), and of the Logical Data Model (LinkML). All of
them ship with the repository. The server runs locally and doesn't connect to
any OSDU environment. It needs no sign-in and builds no index; it starts in a
few seconds.

Repository: <https://github.com/equinor/osdu-mcp-poc>

---

## 1. Install

You need `git` and [uv](https://docs.astral.sh/uv/), which installs the right
Python version itself.

**macOS**

```bash
brew install uv
```

**Windows**

```powershell
winget install --id=astral-sh.uv -e
```

Clone the repository and install the dependencies:

```bash
git clone https://github.com/equinor/osdu-mcp-poc.git
cd osdu-mcp-poc
uv sync
```

Check that it works:

```bash
uv run osdu-query
```

The search builds in a few seconds. Type, for example, `validate legal tags`
and press Enter. Type `quit` to exit.

## 2. Find the two paths you need

The clients start the server themselves, and need full (absolute) paths.

| What | macOS | Windows |
|---|---|---|
| Path to `uv` | `which uv`, often `/opt/homebrew/bin/uv` | `where.exe uv`; use the first line it prints |
| Path to the repository | `pwd` in the repository folder, e.g. `/Users/<you>/osdu-mcp-poc` | `cd` in the repository folder, e.g. `C:\Users\<you>\osdu-mcp-poc` |

Use the full path to `uv`, not just `uv`: the clients don't always find it on
their own.

In the examples below, `UV` stands for the path to `uv`, and `REPO` for the
path to the repository. Replace them with your own. On Windows, write paths in
JSON with forward slashes (`C:/Users/you/osdu-mcp-poc`) or doubled
backslashes (`C:\\Users\\you\\osdu-mcp-poc`).

## 3. Connect your client

### GitHub Copilot CLI (terminal)

Open or create `~/.copilot/mcp-config.json`
(Windows: `%USERPROFILE%\.copilot\mcp-config.json`), and add:

```json
{
  "mcpServers": {
    "osdu-discovery": {
      "type": "local",
      "command": "UV",
      "tools": ["*"],
      "args": [
        "run", "--project", "REPO", "osdu-mcp",
        "--specs", "REPO/specs",
        "--schemas", "REPO/schemas",
        "--model", "REPO/model/osdu_latest.yaml"
      ]
    }
  }
}
```

If the file already lists other servers, add just the
`"osdu-discovery": {...}` entry under the existing `mcpServers`.

Start a **new** `copilot` session and run `/mcp`. `osdu-discovery` should
show as connected, with 8 tools.

### VS Code (Copilot Chat)

Create `.vscode/mcp.json` in a workspace, or run **MCP: Open User
Configuration** from the Command Palette to have the server in every
workspace. Note that VS Code uses `servers` and `"type": "stdio"`:

```json
{
  "servers": {
    "osdu-discovery": {
      "type": "stdio",
      "command": "UV",
      "args": [
        "run", "--project", "REPO", "osdu-mcp",
        "--specs", "REPO/specs",
        "--schemas", "REPO/schemas",
        "--model", "REPO/model/osdu_latest.yaml"
      ]
    }
  }
}
```

Click **Start** above the server in `mcp.json`. Switch Copilot Chat to
**Agent** mode, because the tools aren't used in Ask mode. Under the tools
icon you can check that the tools are enabled.

### Other clients

Claude Desktop, Claude Code, Cursor and OpenCode have ready-made examples in
the repository (`*.example.json`) and in the README, under "Client
configuration".

## 4. Try it

Ask in plain words. The table shows what a good answer mentions, so you can
tell it works. The agent words the answer itself, so look for the key names,
not exact text.

| Question | A good answer mentions | Tool |
|---|---|---|
| Which OSDU API shows the edit history of a record? | `GET /records/versions/{id}`, then `GET /records/{id}/{version}` | `list_operations` |
| Which record type holds formation tops, and which property has the depth? | `WellboreMarkerSet`, `data.Markers[].MarkerMeasuredDepth` | `list_kinds`, `list_properties` |
| Which record types reference a Wellbore? | 61 kinds, for example `WellLicense data.LicenseViolations[].WellboreID` | `get_relationships` |
| How do WellboreMarkerSet and SeismicHorizon relate? | Through `HorizonControlPoints`, or through `HorizonInterpretation` | `find_path` |
| How does a File.Generic connect to a Wellbore? | Through a work-product component that holds both, e.g. `BiostratigraphicStudy` | `find_path` |
| Which Well properties are deprecated? | 10, e.g. `FacilityNameAliases → NameAliases` | `get_deprecations` |

The last three use the Logical Data Model (LinkML): what points *in* to a
kind, routes through a third kind, and deprecations with their replacements.
None of that is stated directly in the JSON Schemas.

The first time a tool is used, the client asks for permission. Approve it.

**Tips for good answers**

- **Start a new session** from an ordinary folder. An old, resumed session,
  or one started in a folder with OSDU source code, makes the agent go and
  read code instead of using the tools, and the answer then takes minutes
  instead of seconds.
- **Ask for the tools** if the agent wanders off: "answer from the
  osdu-discovery tools".
- **Ask in domain words.** You don't need to know the OSDU names; the agent
  looks them up in the catalog.

## The tools

| Tool | What it does |
|---|---|
| `list_operations` | All API operations (366), one line each, grouped by service |
| `list_kinds` | All record types (latest version), with a one-line description. Reference data (code lists) is hidden by default |
| `list_properties` | All properties of a record type: path, type, description, links and unit |
| `search_osdu` | Keyword search over operations and properties, best when you know the name |
| `get_schema` | The full JSON Schema of a record type |
| `get_relationships` | What a record type points to, and what points to it |
| `find_path` | How two record types are connected, step by step |
| `get_deprecations` | Deprecated properties and what replaces them |

## Updating

```bash
cd REPO
git pull
```

Restart the client (or the session) afterwards. Nothing needs building.

## Troubleshooting

| Problem | Fix |
|---|---|
| The server doesn't start, `ENOENT` or `command not found` | `command` must be the full path to `uv`; see step 2. |
| The assistant answers from memory without calling a tool | Name the tool in the question, e.g. "use find_path". |
| No tools appear | Restart the client completely; most read their configuration only at startup. In VS Code, check that you are in Agent mode. |
| A tool answers `... unavailable` and mentions `--specs`, `--schemas` or `--model` | That path is missing or wrong in the configuration. |
| The server exits with `ERROR: nothing loaded` | None of the `--specs`, `--schemas` or `--model` paths existed. Check `REPO`. |
| You want to see what the server does | Run the command from the configuration directly in a terminal. A healthy start prints four lines ending in `enabled.` or `ready.`, then waits. Stop it with Ctrl+C. |

More detail is in [README.md](../README.md) in the repository.
