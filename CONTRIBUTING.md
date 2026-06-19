# Contributing

Thanks for your interest in contributing to `osdu-mcp-poc`! This project provides
semantic search over OSDU OpenAPI specs and schema files, exposed as an MCP
server. Contributions are welcome via issues and pull requests.

## Reporting issues

- Search [existing issues](https://github.com/equinor/osdu-mcp-poc/issues) before
  opening a new one.
- For security vulnerabilities, **do not** open a public issue — follow
  [`SECURITY.md`](SECURITY.md) instead.

## Development setup

This project uses [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                         # install dependencies
uv run osdu-index               # build the local ChromaDB index
uv run osdu-query "<query>"     # query the index
uv run osdu-mcp --db ./chroma_db --schemas ./schemas   # run the MCP server
```

On first run, ChromaDB downloads a local embedding model (~90 MB, once).

## Pull request process

1. Fork the repository (or create a branch if you have write access) and base
   your work on `main`.
2. Make your change and verify the affected commands run locally.
3. Open a pull request against `main`.
4. Use clear, [Conventional Commits](https://www.conventionalcommits.org/)-style
   PR titles (e.g. `feat: add schema provenance tool`, `fix: correct db path`).
5. At least one approving review (including a CODEOWNERS review) is required
   before merging. Direct pushes to `main` are not permitted.
6. Pull requests are merged using **squash merge**.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE).
