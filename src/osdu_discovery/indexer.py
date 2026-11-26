"""
OSDU Discovery PoC — document extraction

Turns OpenAPI spec files and OSDU schema files into text documents: one per
API operation, one per kind and one per schema property, with cross-file
$refs into the abstract schemas resolved. `osdu_discovery.search` indexes
these for `search_osdu`, and `osdu_discovery.catalog` uses the same property
walk for `list_properties`.

The module name is historical: it used to also build a ChromaDB embedding
index. That build now lives in eval/dense_index.py, for re-running the
retrieval evaluation only.
"""

import json
import os
import re

import yaml

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


def resolve_refs(
    obj: dict | list,
    root: dict,
    _seen: frozenset[str] = frozenset(),
) -> dict | list:
    """
    Recursively resolves $ref pointers within the same document.
    Cross-file $refs are left as-is (noted in the text as [external ref]).

    Self-referential schemas are cut off rather than followed. GeoJSON is the
    case that matters here: ``GeoJsonFeature`` and ``GeoJsonGeometryCollection``
    contain themselves, so following every ``$ref`` blindly recurses until the
    interpreter gives up and the whole spec is dropped from the index. ``_seen``
    carries the refs already expanded on the current branch, mirroring the guard
    in ``SchemaResolver.resolve``.
    """
    if isinstance(obj, list):
        return [resolve_refs(item, root, _seen) for item in obj]

    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            if ref.startswith("#/"):
                if ref in _seen:
                    return {"type": "object", "description": f"[recursive ref: {ref}]"}
                # Internal reference — resolve it
                parts = ref.lstrip("#/").split("/")
                resolved = root
                try:
                    for part in parts:
                        resolved = resolved[part]
                    return resolve_refs(resolved, root, _seen | {ref})
                except (KeyError, TypeError):
                    return {"description": f"[unresolved ref: {ref}]"}
            else:
                return {"description": f"[external ref: {ref}]"}
        return {k: resolve_refs(v, root, _seen) for k, v in obj.items()}

    return obj


class SchemaResolver:
    """Resolves JSON-Schema ``$ref`` pointers, including cross-file refs.

    Every OSDU entity puts its real payload inside ``data``, which is an
    ``allOf`` of cross-file ``$ref``s into ``../abstract/*.json``. This
    resolver follows those refs (relative to each referencing file), inlines
    the target, and guards against circular references. Loaded files are
    cached, so resolving the whole tree stays cheap.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def _load(self, path: str) -> dict:
        path = os.path.abspath(path)
        if path not in self._cache:
            self._cache[path] = load_file(path)
        return self._cache[path]

    def resolve(
        self,
        obj: dict | list,
        *,
        base_dir: str,
        root: dict,
        _seen: frozenset[str] = frozenset(),
    ) -> dict | list:
        if isinstance(obj, list):
            return [
                self.resolve(item, base_dir=base_dir, root=root, _seen=_seen)
                for item in obj
            ]
        if not isinstance(obj, dict):
            return obj

        if "$ref" in obj:
            ref = obj["$ref"]
            file_part, _, fragment = ref.partition("#")
            if file_part:
                target_path = os.path.normpath(os.path.join(base_dir, file_part))
                ref_key = f"{target_path}#{fragment}"
            else:
                ref_key = f"{id(root)}#{fragment}"

            if ref_key in _seen:
                return {"type": "object", "description": f"[recursive ref: {ref}]"}

            if file_part:
                try:
                    new_root = self._load(target_path)
                except (OSError, ValueError) as exc:
                    return {"description": f"[unresolved ref: {ref} ({exc})]"}
                new_base = os.path.dirname(target_path)
            else:
                new_root, new_base = root, base_dir

            node: object = new_root
            for part in (p for p in fragment.lstrip("/").split("/") if p):
                try:
                    node = node[part]  # type: ignore[index]
                except (KeyError, TypeError, IndexError):
                    return {"description": f"[unresolved ref: {ref}]"}

            resolved = self.resolve(
                node, base_dir=new_base, root=new_root, _seen=_seen | {ref_key}
            )
            # Keep ref-site siblings (title/description) — they are more contextual.
            siblings = {k: v for k, v in obj.items() if k != "$ref"}
            if isinstance(resolved, dict) and siblings:
                return {**resolved, **siblings}
            return resolved

        return {
            k: self.resolve(v, base_dir=base_dir, root=root, _seen=_seen)
            for k, v in obj.items()
        }


def merged_properties(schema: dict) -> dict:
    """Return ``properties`` with every ``allOf`` subschema merged in.

    Assumes ``$ref``s were already inlined by :class:`SchemaResolver`, so the
    ``allOf`` members carry real ``properties`` instead of bare ``$ref`` dicts.
    """
    props: dict = dict(schema.get("properties", {}))
    for sub in schema.get("allOf", []):
        if isinstance(sub, dict):
            props.update(merged_properties(sub))
    return props


# ---------------------------------------------------------------------------
# OpenAPI spec extraction
# ---------------------------------------------------------------------------

# Generator default titles that don't name the service; fall back to the filename.
_GENERIC_TITLES = {"openapi definition", "api", "openapi"}


def extract_service_name(spec: dict, filename: str) -> str:
    title = spec.get("info", {}).get("title", "").strip()
    if title and title.lower() not in _GENERIC_TITLES:
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


# ---------------------------------------------------------------------------
# OSDU schema extraction
# ---------------------------------------------------------------------------

def extract_kind_string(schema_doc: dict, filename: str) -> str:
    """Extract the full kind identifier from an OSDU schema document."""
    # Generated OSDU schemas carry the canonical kind here, e.g.
    # "osdu:wks:dataset--File.Generic:1.1.0".
    source = schema_doc.get("x-osdu-schema-source", "")
    if source:
        return source

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


def extract_semantic_keywords(prop_name: str, prop_schema: dict) -> list[str]:
    """
    Extract semantic keywords that help with domain-specific retrieval.
    Examples: "ID" suffix → relationship; URLs → dataset linking; arrays → collections
    """
    keywords = []

    # Property name heuristics
    if prop_name.endswith("ID"):
        keywords.append("relationship")
        keywords.append("reference")
        # Try to extract entity type from pattern
        pattern = prop_schema.get("pattern", "")
        if "dataset" in pattern.lower():
            keywords.extend(["dataset", "file", "resource"])
        if "acquisition" in pattern.lower():
            keywords.extend(["acquisition", "collection"])

    if "url" in prop_name.lower() or "uri" in prop_name.lower():
        keywords.extend(["external", "link", "reference", "file"])

    if "path" in prop_name.lower():
        keywords.extend(["file", "location", "path", "reference"])

    if "file" in prop_name.lower():
        keywords.extend(["file", "external", "data", "storage"])

    if "data" in prop_name.lower():
        keywords.extend(["data", "content", "storage"])

    # Type based
    prop_type = prop_schema.get("type", "")
    if prop_type == "array":
        keywords.append("collection")
        keywords.append("multiple")

    # Description keywords
    description = prop_schema.get("description", "").lower()
    for keyword in ["dataset", "file", "external", "link", "reference", "relationship",
                     "connection", "resource", "data", "metadata", "las", "curve"]:
        if keyword in description:
            keywords.append(keyword)

    return list(set(keywords))  # Remove duplicates


def _relationships(prop: dict) -> list[dict]:
    """``x-osdu-relationship`` on the property, or on its array items."""
    rels = prop.get("x-osdu-relationship") or []
    items = prop.get("items")
    if not rels and isinstance(items, dict):
        rels = items.get("x-osdu-relationship") or []
    return [r for r in rels if isinstance(r, dict)]


def _units(prop: dict) -> str:
    """The unit annotation of a property or its array items. The shipped
    schemas use ``x-osdu-frame-of-reference`` ("UOM:length",
    "UOM_via_property:DepthUnit"); the other two are kept for older ones."""
    for schema in (prop, prop.get("items")):
        if not isinstance(schema, dict):
            continue
        for key in ("x-osdu-frame-of-reference", "x-osdu-uom-quantity", "x-unit"):
            value = schema.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _branches(schema: dict) -> list[dict]:
    """oneOf/anyOf branches of a schema, including those inside its allOf
    members (Coring's ConventionalCoring / SidewallCoring sit there)."""
    out = [b for key in ("oneOf", "anyOf") for b in schema.get(key, []) if isinstance(b, dict)]
    for sub in schema.get("allOf", []):
        if isinstance(sub, dict):
            out += _branches(sub)
    return out


# Keys of the GeoJSON geometry and feature variants (Point, LineString, ...)
# that every SpatialLocation repeats as oneOf branches. They are the GeoJSON
# standard, not OSDU data model, and expanding them would add ~12,500 near-
# identical documents to the index.
_GEOJSON_KEYS = {"type", "coordinates", "bbox", "geometries", "geometry", "properties"}


def _is_geojson_branch(branch: dict) -> bool:
    props = merged_properties(branch)
    return bool(props) and set(props) <= _GEOJSON_KEYS


def _has_children(schema: dict) -> bool:
    return any(k in schema for k in ("properties", "allOf", "oneOf", "anyOf"))


def flatten_schema_properties(
    schema: dict,
    prefix: str = "",
    depth: int = 0,
    max_depth: int = 6,
    alternative: bool = False,
) -> list[dict]:
    """
    Walk a JSON Schema object and return a flat list of property entries,
    each with its dotted path, type, and description, enriched with semantic keywords.

    Follows ``allOf`` members, ``oneOf``/``anyOf`` branches (entries from a
    branch get ``alternative=True``: present in only some records) and array
    items (``path[]``). GeoJSON geometry variants are not expanded. Relationships are read from the property or its
    items; a target naming only a group becomes ``group/*``. A path reached
    through several branches is listed once.

    Expects ``$ref``s to have been inlined by :class:`SchemaResolver` first, so
    cross-file ``allOf`` members (the OSDU ``Abstract*`` schemas) contribute
    their real properties.
    """
    results: list[dict] = []
    if depth > max_depth:
        return results

    groups = [(merged_properties(schema), alternative)]
    groups += [(merged_properties(b), True) for b in _branches(schema) if not _is_geojson_branch(b)]

    for properties, alt in groups:
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_schema, dict):
                continue
            path = f"{prefix}.{prop_name}" if prefix else prop_name
            prop_type = prop_schema.get("type", "object")
            description = prop_schema.get("description", "").strip()
            enum_vals = prop_schema.get("enum", [])
            pattern = prop_schema.get("pattern", "")

            # Extract semantic keywords for better retrieval
            semantic_keywords = extract_semantic_keywords(prop_name, prop_schema)

            # OSDU relationships, raw (group, entity) and as "group/entity" text
            relationships: list[tuple[str, str]] = []
            related_types: list[str] = []
            for rel in _relationships(prop_schema):
                group = rel.get("GroupType", "")
                entity = rel.get("EntityType", "")
                if not (group or entity):
                    continue
                relationships.append((group, entity))
                related_types.append(f"{group}/{entity or '*'}".lower())
                semantic_keywords.append((entity or group).lower())

            results.append({
                "path": path,
                "type": prop_type,
                "description": description,
                "units": _units(prop_schema),
                "enum": enum_vals,
                "pattern": pattern,
                "semantic_keywords": list(dict.fromkeys(semantic_keywords)),
                "related_types": list(dict.fromkeys(related_types)),
                "relationships": list(dict.fromkeys(relationships)),
                "alternative": alt,
            })

            # Recurse into nested objects (and their oneOf/anyOf branches)
            if _has_children(prop_schema):
                results.extend(flatten_schema_properties(prop_schema, path, depth + 1, max_depth, alt))

            # Recurse into array items
            items = prop_schema.get("items", {})
            if isinstance(items, dict) and _has_children(items):
                results.extend(flatten_schema_properties(items, f"{path}[]", depth + 1, max_depth, alt))

    if depth == 0:
        # A path reached through several branches is listed once; it is an
        # alternative only if every route to it is.
        merged: dict[str, dict] = {}
        for entry in results:
            if entry["path"] in merged:
                merged[entry["path"]]["alternative"] &= entry["alternative"]
            else:
                merged[entry["path"]] = entry
        results = list(merged.values())
    return results


def data_schema(resolved_doc: dict) -> tuple[dict, str]:
    """The schema holding a kind's data model, and the path prefix for its
    properties: ``data`` for most kinds; the root for content kinds, which
    have no ``data`` wrapper."""
    data = resolved_doc.get("properties", {}).get("data")
    return (data, "data") if isinstance(data, dict) else (resolved_doc, "")


def format_kind_summary(resolved_doc: dict, kind: str) -> str:
    """Build the kind-level summary text. ``resolved_doc`` must already have
    its ``$ref``s inlined by :class:`SchemaResolver`."""
    description = (
        resolved_doc.get("schemaInfo", {}).get("description", "")
        or resolved_doc.get("description", "")
    ).strip()

    schema, _ = data_schema(resolved_doc)
    top_level_props = list(merged_properties(schema).keys())
    props_str = ", ".join(top_level_props[:25])  # cap for readability

    return "\n".join(filter(None, [
        f"Kind: {kind}",
        f"Description: {description}" if description else "",
        f"Top-level data properties: {props_str}" if props_str else "",
    ]))


def extract_schema_entries(
    schema_doc: dict,
    kind: str,
    resolver: SchemaResolver,
    file_path: str,
) -> list[dict]:
    entries = []

    # Inline every $ref (including cross-file refs into ../abstract/*.json)
    # once, up front, so both the summary and the property walk see the
    # fully-expanded data model.
    base_dir = os.path.dirname(os.path.abspath(file_path))
    resolved = resolver.resolve(schema_doc, base_dir=base_dir, root=schema_doc)
    if not isinstance(resolved, dict):
        resolved = schema_doc

    # Kind-level entry (for broad queries)
    kind_text = format_kind_summary(resolved, kind)
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
    schema, prefix = data_schema(resolved)
    flat_props = flatten_schema_properties(schema, prefix)

    for prop in flat_props:
        path = prop["path"]
        prop_type = prop["type"]
        description = prop["description"]
        units = prop["units"]
        enum_vals = prop["enum"]
        semantic_keywords = prop.get("semantic_keywords", [])
        related_types = prop.get("related_types", [])

        text_parts = [
            f"Kind: {kind}",
            f"Property path: {path}",
            f"Type: {prop_type}",
        ]
        if description:
            text_parts.append(f"Description: {description}")
        if units:
            text_parts.append(f"Unit of measure: {units}")
        if enum_vals:
            text_parts.append(f"Allowed values: {enum_vals}")
        
        # Add semantic keywords to improve retrieval
        if semantic_keywords:
            text_parts.append(f"Keywords: {', '.join(semantic_keywords)}")
        
        # Add relationship information for better domain discovery
        if related_types:
            text_parts.append(f"Relates to: {', '.join(related_types)}")
        if prop["alternative"]:
            text_parts.append("Present in only some records (one of several alternative structures)")

        id_path = path.removeprefix("data.")  # ids unchanged for data-wrapped kinds
        prop_id = f"prop__{kind}__{id_path.replace('.', '_').replace('[]', '_arr')}"
        # ChromaDB ids must be under 512 chars
        if len(prop_id) > 500:
            prop_id = prop_id[:500]

        entries.append({
            "id": prop_id,
            "text": "\n".join(text_parts),
            "metadata": {
                "type": "property",
                "kind": kind,
                "path": path,
            },
        })

    return entries
