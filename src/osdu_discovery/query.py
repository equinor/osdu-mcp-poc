"""
OSDU Discovery PoC — Query CLI

Interactive query tool for testing semantic search quality against
the indexed ChromaDB collections produced by the indexer.

Usage:
    uv run -m osdu_discovery.query --db ./chroma_db
    uv run -m osdu_discovery.query --db ./chroma_db --top 8
    uv run -m osdu_discovery.query --db ./chroma_db --mode api
    uv run -m osdu_discovery.query --db ./chroma_db --mode schema

Or via the installed script entry point:
    osdu-query --db ./chroma_db
"""

import argparse
import sys
from itertools import zip_longest

import chromadb
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich import box

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


def score_label(distance: float) -> str:
    """
    ChromaDB cosine distance: 0 = identical, 2 = opposite.
    Convert to a rough relevance label.
    """
    if distance < 0.3:
        return "[bold green]strong[/bold green]"
    if distance < 0.5:
        return "[green]good[/green]"
    if distance < 0.7:
        return "[yellow]weak[/yellow]"
    return "[red]poor[/red]"


def query_collection(
    collection: chromadb.Collection,
    query: str,
    top_k: int,
    source_tag: str,
) -> list[dict]:
    try:
        results = collection.query(query_texts=[query], n_results=top_k)
    except Exception as e:
        console.print(f"[red]Query error ({source_tag}): {e}[/red]")
        return []

    hits = []
    for i in range(len(results["ids"][0])):
        hits.append({
            "source":   source_tag,
            "id":       results["ids"][0][i],
            "text":     results["documents"][0][i],
            "distance": results["distances"][0][i],
            "metadata": results["metadatas"][0][i],
        })
    return hits


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
        distance = hit["distance"]
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

        relevance = score_label(distance)
        distance_str = f"{distance:.3f}"

        panel_title = f"[bold]#{rank}[/bold]  {header}   relevance: {relevance} ({distance_str})"
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


def print_stats(api_col, schema_col, mode: str) -> None:
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Collection")
    table.add_column("Entries", justify="right")
    table.add_column("Active")

    if api_col:
        table.add_row(
            "osdu_api_operations",
            str(api_col.count()),
            "✓" if mode in ("both", "api") else "—",
        )
    if schema_col:
        table.add_row(
            "osdu_schema_properties",
            str(schema_col.count()),
            "✓" if mode in ("both", "schema") else "—",
        )

    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Query the OSDU semantic index")
    parser.add_argument("--db",   default="./chroma_db", help="ChromaDB persistence folder")
    parser.add_argument("--top",  type=int, default=5,   help="Number of results to return (default: 5)")
    parser.add_argument(
        "--mode",
        choices=["both", "api", "schema"],
        default="both",
        help="Which collection(s) to query (default: both)",
    )
    args = parser.parse_args()

    client = chromadb.PersistentClient(path=args.db)

    # Load collections that exist
    existing = {c.name for c in client.list_collections()}

    api_col = None
    schema_col = None

    if args.mode in ("both", "api"):
        if "osdu_api_operations" in existing:
            api_col = client.get_collection("osdu_api_operations")
        else:
            console.print("[yellow]Collection 'osdu_api_operations' not found — run osdu-index --specs first.[/yellow]")

    if args.mode in ("both", "schema"):
        if "osdu_schema_properties" in existing:
            schema_col = client.get_collection("osdu_schema_properties")
        else:
            console.print("[yellow]Collection 'osdu_schema_properties' not found — run osdu-index --schemas first.[/yellow]")

    if not api_col and not schema_col:
        console.print("[red]No collections available. Exiting.[/red]")
        sys.exit(1)

    console.print()
    console.print(Panel(
        f"[bold]OSDU Discovery — Semantic Search[/bold]\n"
        f"Mode: {MODE_LABELS[args.mode]}   Top-K: {args.top}\n\n"
        "Type a natural language need and see which endpoints/schemas match.\n"
        "Commands:  [bold]stats[/bold] — show index counts   [bold]quit[/bold] / [bold]exit[/bold] — exit",
        border_style="cyan",
    ))
    print_stats(api_col, schema_col, args.mode)

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
            print_stats(api_col, schema_col, args.mode)
            continue

        hits = []
        if api_col and schema_col:
            # Both collections active: guarantee representation from each.
            # Fetch top_k from each independently, then interleave by rank
            # so neither collection drowns the other out by volume.
            api_hits    = query_collection(api_col,    query, args.top, source_tag="api")
            schema_hits = query_collection(schema_col, query, args.top, source_tag="schema")
            # Interleave: api[0], schema[0], api[1], schema[1], ...
            for pair in zip_longest(api_hits, schema_hits):
                hits.extend(h for h in pair if h is not None)
        elif api_col:
            hits = query_collection(api_col, query, args.top, source_tag="api")
        elif schema_col:
            hits = query_collection(schema_col, query, args.top, source_tag="schema")

        render_results(hits, query, top_k=args.top)


if __name__ == "__main__":
    main()
