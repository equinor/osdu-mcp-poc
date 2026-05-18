"""
OSDU Discovery PoC — Indexer

Reads OpenAPI spec files and OSDU schema files from local folders,
extracts meaningful text representations of each operation / schema property,
and stores them in persistent ChromaDB collections for semantic search.

Usage:
    uv run -m osdu_discovery.indexer --specs ./specs --schemas ./schemas --db ./chroma_db
    uv run -m osdu_discovery.indexer --specs ./specs --db ./chroma_db          # specs only
    uv run -m osdu_discovery.indexer --schemas ./schemas --db ./chroma_db      # schemas only
    uv run -m osdu_discovery.indexer --specs ./specs --schemas ./schemas --db ./chroma_db --reset

Or via the installed script entry point:
    osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db
"""

import argparse
import json
import os
import re
import sys

import chromadb
import yaml
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_file(filepath: str) -> dict:
    with open(filepath, encoding="utf-8") as f:
        if filepath.endswith((".yaml", ".yml")):
            return yaml.safe_load(f)
        return json.load(f)


def is_spec_file(filename: str) -> bool:
    return filename.endswith((".yaml", ".yml", ".json"))


def resolve_refs(obj: dict | list, root: dict) -> dict | list:
    """
    Recursively resolves $ref pointers within the same document.
    Cross-file $refs are left as-is (noted in the text as [external ref]).
    """
    if isinstance(obj, list):
        return [resolve_refs(item, root) for item in obj]

    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            if ref.startswith("#/"):
                # Internal reference — resolve it
                parts = ref.lstrip("#/").split("/")
                resolved = root
                try:
                    for part in parts:
                        resolved = resolved[part]
                    return resolve_refs(resolved, root)
                except (KeyError, TypeError):
                    return {"description": f"[unresolved ref: {ref}]"}
            else:
                return {"description": f"[external ref: {ref}]"}
        return {k: resolve_refs(v, root) for k, v in obj.items()}

    return obj


# ---------------------------------------------------------------------------
# OpenAPI spec extraction
# ---------------------------------------------------------------------------

def extract_service_name(spec: dict, filename: str) -> str:
    title = spec.get("info", {}).get("title", "")
    if title:
        return title
    # Fall back to filename without extension
    return re.sub(r"\.(yaml|yml|json)$", "", filename, flags=re.IGNORECASE)


def format_parameters(parameters: list) -> str:
    if not parameters:
        return ""
    lines = []
    for p in parameters:
        name = p.get("name", "unknown")
        location = p.get("in", "")
        required = "required" if p.get("required") else "optional"
        description = p.get("description", "").strip()
        schema = p.get("schema", {})
        param_type = schema.get("type", schema.get("$ref", "any"))
        enum_vals = schema.get("enum", [])
        enum_str = f", one of: {enum_vals}" if enum_vals else ""
        lines.append(f"  - {name} ({location}, {required}, {param_type}{enum_str}): {description}")
    return "Parameters:\n" + "\n".join(lines)


def format_properties(properties: dict, depth: int = 0, max_depth: int = 3) -> list[str]:
    """Recursively format object properties up to max_depth."""
    lines = []
    indent = "  " * depth
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        prop_type = prop_schema.get("type", "object")
        description = prop_schema.get("description", "").strip()
        lines.append(f"{indent}  - {prop_name} ({prop_type}): {description}")
        # Recurse into nested objects
        if depth < max_depth and prop_type == "object":
            nested = prop_schema.get("properties", {})
            if nested:
                lines.extend(format_properties(nested, depth + 1, max_depth))
        # Recurse into array items if they are objects
        if depth < max_depth and prop_type == "array":
            items = prop_schema.get("items", {})
            nested = items.get("properties", {})
            if nested:
                lines.append(f"{indent}    (array items):")
                lines.extend(format_properties(nested, depth + 1, max_depth))
    return lines


def format_request_body(request_body: dict, root: dict) -> str:
    if not request_body:
        return ""
    description = request_body.get("description", "").strip()
    content = request_body.get("content", {})
    schema = (
        content.get("application/json", {}).get("schema", {})
        or content.get("*/*", {}).get("schema", {})
    )
    schema = resolve_refs(schema, root)
    properties = schema.get("properties", {})

    # Handle allOf
    if not properties and "allOf" in schema:
        for sub in schema["allOf"]:
            sub = resolve_refs(sub, root)
            properties.update(sub.get("properties", {}))

    if not properties:
        return f"Request body: {description}" if description else ""

    lines = [f"Request body{': ' + description if description else ''}:"]
    lines.extend(format_properties(properties))
    return "\n".join(lines)


def format_responses(responses: dict) -> str:
    if not responses:
        return ""
    lines = []
    for status_code, response in responses.items():
        description = response.get("description", "").strip()
        lines.append(f"Response {status_code}: {description}")
    return "\n".join(lines)


def extract_api_operations(spec: dict, service_name: str) -> list[dict]:
    operations = []
    paths = spec.get("paths", {})

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        # Path-level parameters apply to all methods
        path_params = path_item.get("parameters", [])
        path_params = [resolve_refs(p, spec) for p in path_params]

        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            operation_id = operation.get("operationId", f"{method}_{path}")
            summary = operation.get("summary", "").strip()
            description = operation.get("description", "").strip()
            tags = ", ".join(operation.get("tags", []))

            op_params = [resolve_refs(p, spec) for p in operation.get("parameters", [])]
            all_params = path_params + op_params
            parameters_text = format_parameters(all_params)

            request_body_text = format_request_body(
                operation.get("requestBody", {}), spec
            )
            responses_text = format_responses(operation.get("responses", {}))

            text = "\n".join(filter(None, [
                f"Service: {service_name}",
                f"Method: {method.upper()}",
                f"Path: {path}",
                f"Summary: {summary}",
                f"Description: {description}" if description else "",
                f"Tags: {tags}" if tags else "",
                parameters_text,
                request_body_text,
                responses_text,
            ]))

            operations.append({
                "id": f"{service_name}__{operation_id}",
                "text": text,
                "metadata": {
                    "service": service_name,
                    "method": method.upper(),
                    "path": path,
                    "operation_id": operation_id,
                    "summary": summary,
                },
            })

    return operations


def index_specs(specs_folder: str, collection: chromadb.Collection) -> int:
    total = 0
    files = [f for f in os.listdir(specs_folder) if is_spec_file(f)]
    if not files:
        console.print(f"[yellow]No spec files found in {specs_folder}[/yellow]")
        return 0

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        for filename in files:
            task = progress.add_task(f"Indexing spec: {filename}", total=None)
            filepath = os.path.join(specs_folder, filename)
            try:
                spec = load_file(filepath)
                service_name = extract_service_name(spec, filename)
                operations = extract_api_operations(spec, service_name)

                if operations:
                    collection.upsert(
                        ids=[op["id"] for op in operations],
                        documents=[op["text"] for op in operations],
                        metadatas=[op["metadata"] for op in operations],
                    )
                    progress.update(
                        task,
                        description=f"[green]✓[/green] {service_name}: {len(operations)} operations"
                    )
                    total += len(operations)
                else:
                    progress.update(task, description=f"[yellow]⚠[/yellow] {filename}: no operations found")
            except Exception as e:
                progress.update(task, description=f"[red]✗[/red] {filename}: {e}")

    return total


# ---------------------------------------------------------------------------
# OSDU schema extraction
# ---------------------------------------------------------------------------

def extract_kind_string(schema_doc: dict, filename: str) -> str:
    """Extract the full kind identifier from an OSDU schema document."""
    schema_info = schema_doc.get("schemaInfo", {})
    identity = schema_info.get("schemaIdentity", {})
    kind_id = identity.get("id", "")
    if kind_id:
        return kind_id
    # Reconstruct from parts if id is absent
    authority = identity.get("authority", "")
    source = identity.get("source", "")
    entity = identity.get("entityType", "")
    major = identity.get("schemaVersionMajor", 0)
    minor = identity.get("schemaVersionMinor", 0)
    patch = identity.get("schemaVersionPatch", 0)
    if authority and source and entity:
        return f"{authority}:{source}:{entity}:{major}.{minor}.{patch}"
    return re.sub(r"\.(yaml|yml|json)$", "", filename, flags=re.IGNORECASE)


def flatten_schema_properties(
    schema: dict,
    prefix: str = "",
    depth: int = 0,
    max_depth: int = 4,
) -> list[dict]:
    """
    Walk a JSON Schema object and return a flat list of property entries,
    each with its dotted path, type, and description.
    """
    results = []
    if depth > max_depth:
        return results

    properties = schema.get("properties", {})

    # Merge allOf subschemas
    for sub in schema.get("allOf", []):
        if isinstance(sub, dict):
            properties.update(sub.get("properties", {}))

    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        path = f"{prefix}.{prop_name}" if prefix else prop_name
        prop_type = prop_schema.get("type", "object")
        description = prop_schema.get("description", "").strip()
        units = prop_schema.get("x-osdu-uom-quantity", prop_schema.get("x-unit", ""))
        enum_vals = prop_schema.get("enum", [])
        pattern = prop_schema.get("pattern", "")

        results.append({
            "path": path,
            "type": prop_type,
            "description": description,
            "units": units,
            "enum": enum_vals,
            "pattern": pattern,
        })

        # Recurse into nested objects
        if prop_type == "object" or "properties" in prop_schema or "allOf" in prop_schema:
            results.extend(flatten_schema_properties(prop_schema, path, depth + 1, max_depth))

        # Recurse into array items
        if prop_type == "array":
            items = prop_schema.get("items", {})
            if isinstance(items, dict) and ("properties" in items or "allOf" in items):
                results.extend(flatten_schema_properties(items, f"{path}[]", depth + 1, max_depth))

    return results


def format_kind_summary(schema_doc: dict, kind: str) -> str:
    schema_info = schema_doc.get("schemaInfo", {})
    description = (
        schema_info.get("description", "")
        or schema_doc.get("description", "")
    ).strip()

    # Resolve the inner schema
    inner_schema = schema_doc.get("schema", schema_doc)
    inner_schema = resolve_refs(inner_schema, inner_schema)

    # Gather top-level data properties (one level only for summary)
    data_schema = (
        inner_schema.get("properties", {})
                    .get("data", {})
    )
    data_schema = resolve_refs(data_schema, inner_schema)

    top_level_props = []
    for sub in data_schema.get("allOf", []):
        top_level_props.extend(sub.get("properties", {}).keys())
    top_level_props.extend(data_schema.get("properties", {}).keys())
    props_str = ", ".join(top_level_props[:20])  # cap for readability

    return "\n".join(filter(None, [
        f"Kind: {kind}",
        f"Description: {description}" if description else "",
        f"Top-level data properties: {props_str}" if props_str else "",
    ]))


def extract_schema_entries(schema_doc: dict, kind: str) -> list[dict]:
    entries = []

    # Kind-level entry (for broad queries)
    kind_text = format_kind_summary(schema_doc, kind)
    entries.append({
        "id": f"kind__{kind}",
        "text": kind_text,
        "metadata": {
            "type": "kind",
            "kind": kind,
            "path": "",
        },
    })

    # Property-level entries (for specific queries)
    inner_schema = schema_doc.get("schema", schema_doc)
    inner_schema = resolve_refs(inner_schema, inner_schema)

    data_schema = (
        inner_schema.get("properties", {})
                    .get("data", inner_schema)
    )
    flat_props = flatten_schema_properties(data_schema)

    for prop in flat_props:
        path = prop["path"]
        prop_type = prop["type"]
        description = prop["description"]
        units = prop["units"]
        enum_vals = prop["enum"]

        text_parts = [
            f"Kind: {kind}",
            f"Property path: data.{path}",
            f"Type: {prop_type}",
        ]
        if description:
            text_parts.append(f"Description: {description}")
        if units:
            text_parts.append(f"Unit of measure: {units}")
        if enum_vals:
            text_parts.append(f"Allowed values: {enum_vals}")

        prop_id = f"prop__{kind}__{path.replace('.', '_').replace('[]', '_arr')}"
        # ChromaDB ids must be under 512 chars
        if len(prop_id) > 500:
            prop_id = prop_id[:500]

        entries.append({
            "id": prop_id,
            "text": "\n".join(text_parts),
            "metadata": {
                "type": "property",
                "kind": kind,
                "path": f"data.{path}",
            },
        })

    return entries


def index_schemas(schemas_folder: str, collection: chromadb.Collection) -> int:
    total = 0
    files = [f for f in os.listdir(schemas_folder) if is_spec_file(f)]
    if not files:
        console.print(f"[yellow]No schema files found in {schemas_folder}[/yellow]")
        return 0

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        for filename in files:
            task = progress.add_task(f"Indexing schema: {filename}", total=None)
            filepath = os.path.join(schemas_folder, filename)
            try:
                schema_doc = load_file(filepath)
                kind = extract_kind_string(schema_doc, filename)
                entries = extract_schema_entries(schema_doc, kind)

                if entries:
                    collection.upsert(
                        ids=[e["id"] for e in entries],
                        documents=[e["text"] for e in entries],
                        metadatas=[e["metadata"] for e in entries],
                    )
                    progress.update(
                        task,
                        description=f"[green]✓[/green] {kind}: {len(entries)} entries"
                    )
                    total += len(entries)
                else:
                    progress.update(task, description=f"[yellow]⚠[/yellow] {filename}: no entries extracted")
            except Exception as e:
                progress.update(task, description=f"[red]✗[/red] {filename}: {e}")

    return total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Index OSDU specs and schemas into ChromaDB")
    parser.add_argument("--specs",   help="Folder containing OpenAPI spec files")
    parser.add_argument("--schemas", help="Folder containing OSDU schema JSON files")
    parser.add_argument("--db",      default="./chroma_db", help="ChromaDB persistence folder (default: ./chroma_db)")
    parser.add_argument("--reset",   action="store_true",   help="Drop and recreate collections before indexing")
    args = parser.parse_args()

    if not args.specs and not args.schemas:
        console.print("[red]Provide at least --specs or --schemas (or both)[/red]")
        sys.exit(1)

    client = chromadb.PersistentClient(path=args.db)
    console.print(f"\n[bold]ChromaDB:[/bold] {os.path.abspath(args.db)}\n")

    summary = Table(show_header=True, header_style="bold cyan")
    summary.add_column("Collection")
    summary.add_column("Entries indexed", justify="right")

    if args.specs:
        if args.reset:
            try:
                client.delete_collection("osdu_api_operations")
            except Exception:
                pass
        api_col = client.get_or_create_collection(
            "osdu_api_operations",
            metadata={"hnsw:space": "cosine"},
        )
        console.rule("[bold cyan]Indexing API operations[/bold cyan]")
        count = index_specs(args.specs, api_col)
        summary.add_row("osdu_api_operations", str(count))

    if args.schemas:
        if args.reset:
            try:
                client.delete_collection("osdu_schema_properties")
            except Exception:
                pass
        schema_col = client.get_or_create_collection(
            "osdu_schema_properties",
            metadata={"hnsw:space": "cosine"},
        )
        console.rule("[bold cyan]Indexing schema properties[/bold cyan]")
        count = index_schemas(args.schemas, schema_col)
        summary.add_row("osdu_schema_properties", str(count))

    console.print()
    console.rule("[bold green]Done[/bold green]")
    console.print(summary)
    console.print(f"\nRun [bold]osdu-query --db {args.db}[/bold] to test retrieval.\n")


if __name__ == "__main__":
    main()
