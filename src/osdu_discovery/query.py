"""
OSDU Discovery PoC — Query CLI

Interactive tool for trying `search_osdu`'s keyword search. The index is
built in memory from the spec and schema folders in a few seconds.

Usage:
    uv run -m osdu_discovery.query
    uv run -m osdu_discovery.query --top 8
    uv run -m osdu_discovery.query --mode api
    uv run -m osdu_discovery.query --specs ./specs --schemas ./schemas

Or via the installed script entry point:
    osdu-query
"""

import argparse
import os
import sys
import time
from itertools import zip_longest

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich import box

from osdu_discovery.search import SearchIndex

console = Console()

RESULT_COLORS = {
    "api":      "cyan",
    "kind":     "green",
    "property": "yellow",
}

MODE_LABELS = {
    "both":   "API operations + Schema properties",
    "api":    "API operations only",
    "schema": "Schema properties only",
}


def render_results(hits: list[dict], query: str, top_k: int) -> None:
    if not hits:
        console.print("[yellow]No results returned.[/yellow]")
        return

    hits = hits[:top_k * 2]  # cap total shown

    console.print()
    console.print(Rule(f"[bold]Top {len(hits)} results for:[/bold] {query}"))

    for rank, hit in enumerate(hits, start=1):
        source = hit["source"]
        meta = hit["metadata"]
        color = RESULT_COLORS.get(meta.get("type", source), "white")

        # Build header line depending on result type
        if source == "api":
            method  = meta.get("method", "")
            path    = meta.get("path", "")
            service = meta.get("service", "")
            summary = meta.get("summary", "")
            header  = f"[{color}][API][/{color}]  [{color}]{method} {path}[/{color}]  ({service})"
            subtitle = summary
        elif meta.get("type") == "kind":
            kind   = meta.get("kind", "")
            header = f"[{color}][SCHEMA / kind][/{color}]  [{color}]{kind}[/{color}]"
            subtitle = ""
        else:
            kind = meta.get("kind", "")
            path = meta.get("path", "")
            header = f"[{color}][SCHEMA / property][/{color}]  [{color}]{path}[/{color}]"
            subtitle = f"from kind: {kind}"

        versions = f"  [dim](+{hit['versions'] - 1} older versions)[/dim]" if hit["versions"] > 1 else ""
        panel_title = f"[bold]#{rank}[/bold]  {header}   score: {hit['score']:.1f}{versions}"
        panel_body_lines = []
        if subtitle:
            panel_body_lines.append(f"[dim]{subtitle}[/dim]")

        # Show the full indexed text (truncated for readability)
        text_preview = hit["text"]
        if len(text_preview) > 600:
            text_preview = text_preview[:600] + "\n[dim]...[/dim]"
        panel_body_lines.append(text_preview)

        console.print(Panel(
            "\n".join(panel_body_lines),
            title=panel_title,
            title_align="left",
            border_style=color,
            box=box.ROUNDED,
            padding=(0, 1),
        ))

    console.print()


def print_stats(index: SearchIndex, mode: str) -> None:
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Documents")
    table.add_column("Entries", justify="right")
    table.add_column("Active")
    if index.api:
        table.add_row("API operations", str(len(index.api.docs)), "✓" if mode in ("both", "api") else "—")
    if index.schema:
        table.add_row("Schema kinds and properties", str(len(index.schema.docs)), "✓" if mode in ("both", "schema") else "—")
    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Query the OSDU keyword search")
    parser.add_argument("--specs", default="./specs", help="OpenAPI spec folder (default: ./specs)")
    parser.add_argument("--schemas", default="./schemas", help="OSDU schema folder (default: ./schemas)")
    parser.add_argument("--top", type=int, default=5, help="Number of results to return (default: 5)")
    parser.add_argument(
        "--mode",
        choices=["both", "api", "schema"],
        default="both",
        help="Which documents to query (default: both)",
    )
    args = parser.parse_args()

    specs = args.specs if args.mode in ("both", "api") and os.path.isdir(args.specs) else None
    schemas = args.schemas if args.mode in ("both", "schema") and os.path.isdir(args.schemas) else None
    if not specs and not schemas:
        console.print("[red]No spec or schema folder found. Pass --specs and/or --schemas.[/red]")
        sys.exit(1)

    t0 = time.time()
    with console.status("Building the search index..."):
        index = SearchIndex.build(specs, schemas)
    for err in index.errors:
        console.print(f"[yellow]Skipped {err}[/yellow]")

    console.print()
    console.print(Panel(
        f"[bold]OSDU Discovery — Keyword Search[/bold]\n"
        f"Mode: {MODE_LABELS[args.mode]}   Top-K: {args.top}   Built in {time.time() - t0:.1f}s\n\n"
        "Type what you need and see which endpoints/schemas match.\n"
        "Commands:  [bold]stats[/bold] — show index counts   [bold]quit[/bold] / [bold]exit[/bold] — exit",
        border_style="cyan",
    ))
    print_stats(index, args.mode)

    while True:
        try:
            console.print()
            query = console.input("[bold cyan]Query:[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            break

        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            console.print("[dim]Bye.[/dim]")
            break
        if query.lower() == "stats":
            print_stats(index, args.mode)
            continue

        # Fetch top_k from each collection, then interleave by rank so
        # neither drowns the other out by volume.
        api_hits = index.search(query, "api", args.top) if specs else []
        schema_hits = index.search(query, "schema", args.top) if schemas else []
        hits = [h for pair in zip_longest(api_hits, schema_hits) for h in pair if h is not None]
        render_results(hits, query, top_k=args.top)


if __name__ == "__main__":
    main()
