"""Compact catalogs of OSDU API operations, kinds and properties.

Backs the `list_operations`, `list_kinds` and `list_properties` tools. They
let the agent read a short list and pick, instead of relying on a search
index to surface the right item. The evaluation in eval/README.md found this
gets the right answer first far more often than the embedding search.

Everything is built from the spec and schema folders in a few seconds; no
index is needed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from osdu_discovery.indexer import (
    SchemaResolver,
    extract_api_operations,
    extract_service_name,
    is_spec_file,
    load_file,
    merged_properties,
)

REFERENCE_GROUP = "reference-data"
NO_GROUP = "(no group)"  # kinds such as osdu:wks:Manifest:1.0.0

# The group is optional: a few shipped kinds (Manifest, DataCollection) have none.
_KIND_RE = re.compile(
    r"^[^:]+:[^:]+:(?:(?P<group>[a-z-]+)--)?(?P<name>[^:]+):(?P<ver>\d+\.\d+\.\d+)$",
    re.IGNORECASE,
)


def first_sentence(text: str, limit: int) -> str:
    """The first sentence of a description, cut to `limit` characters."""
    text = " ".join((text or "").split())
    sentence = re.split(r"(?<=[a-z0-9)])(?<!e\.g)(?<!i\.e)\.\s", text, maxsplit=1)[0].rstrip(".")
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


@dataclass
class Operation:
    service: str
    method: str
    path: str
    summary: str


@dataclass
class Kind:
    group: str
    name: str
    version: tuple[int, int, int]
    kind_id: str
    description: str
    path: str

    @property
    def short(self) -> str:
        """'master-data--Wellbore' (or 'Manifest' for a kind with no group),
        the form the other tools accept."""
        return f"{self.group}--{self.name}" if self.group else self.name


@dataclass
class Prop:
    path: str  # as displayed and filtered: "data.Curves[].Mnemonic"
    type: str
    description: str
    targets: list[str]  # "master-data--Wellbore", or "any dataset record"
    unit: str  # x-osdu-frame-of-reference, e.g. "UOM:length"
    alternative: bool  # inside a oneOf/anyOf branch: present in only some records


def _relationships(prop: dict) -> list[dict]:
    """x-osdu-relationship on the property, or on its array items."""
    rels = prop.get("x-osdu-relationship") or []
    items = prop.get("items")
    if not rels and isinstance(items, dict):
        rels = items.get("x-osdu-relationship") or []
    return [r for r in rels if isinstance(r, dict)]


def _frame_of_reference(prop: dict) -> str:
    unit = prop.get("x-osdu-frame-of-reference", "")
    items = prop.get("items")
    if not unit and isinstance(items, dict):
        unit = items.get("x-osdu-frame-of-reference", "")
    return unit if isinstance(unit, str) else ""


def _branches(schema: dict) -> list[dict]:
    """oneOf/anyOf branches of a schema, including those inside its allOf
    members (Coring's ConventionalCoring / SidewallCoring sit there)."""
    out = [b for key in ("oneOf", "anyOf") for b in schema.get(key, []) if isinstance(b, dict)]
    for sub in schema.get("allOf", []):
        if isinstance(sub, dict):
            out += _branches(sub)
    return out


def _has_children(schema: dict) -> bool:
    return any(k in schema for k in ("properties", "allOf", "oneOf", "anyOf"))


def walk_properties(
    schema: dict, prefix: str, alternative: bool = False, depth: int = 0, max_depth: int = 8
) -> list[tuple[str, dict, bool]]:
    """(path, property schema, in-alternative) for every property, following
    allOf, oneOf/anyOf branches and array items. Expects resolved $refs."""
    if depth > max_depth:
        return []
    found: list[tuple[str, dict, bool]] = []
    groups = [(merged_properties(schema), alternative)]
    groups += [(merged_properties(b), True) for b in _branches(schema)]
    for props, alt in groups:
        for name, prop in props.items():
            if not isinstance(prop, dict):
                continue
            path = f"{prefix}.{name}" if prefix else name
            found.append((path, prop, alt))
            if _has_children(prop):
                found += walk_properties(prop, path, alt, depth + 1, max_depth)
            items = prop.get("items")
            if isinstance(items, dict) and _has_children(items):
                found += walk_properties(items, f"{path}[]", alt, depth + 1, max_depth)
    return found


@dataclass
class Catalog:
    operations: list[Operation] = field(default_factory=list)
    kinds: dict[str, Kind] = field(default_factory=dict)  # short name (lower) -> latest version
    _resolver: SchemaResolver = field(default_factory=SchemaResolver)

    # ----------------------------------------------------------- loading

    def load_specs(self, specs_dir: str) -> int:
        """Load every spec in the folder. A file that fails to parse is
        skipped and its name kept in `spec_errors`, so one bad file doesn't
        stop the server."""
        self.spec_errors: list[str] = []
        for fn in sorted(os.listdir(specs_dir)):
            if not is_spec_file(fn):
                continue
            try:
                spec = load_file(os.path.join(specs_dir, fn))
                service = extract_service_name(spec, fn)
                ops = extract_api_operations(spec, service)
            except Exception as exc:  # noqa: BLE001 - same tolerance as the indexer
                self.spec_errors.append(f"{fn}: {exc}")
                continue
            for op in ops:
                m = op["metadata"]
                self.operations.append(Operation(m["service"], m["method"], m["path"], m["summary"]))
        return len(self.operations)

    def add_schema(self, path: str, doc: dict) -> None:
        """Register one schema file; keeps the highest version of each kind."""
        m = _KIND_RE.match(doc.get("x-osdu-schema-source", ""))
        if not m:
            return
        ver = tuple(int(x) for x in m["ver"].split("."))
        kind = Kind(m["group"] or "", m["name"], ver, m[0], doc.get("description", ""), path)
        key = kind.short.lower()
        if key not in self.kinds or ver > self.kinds[key].version:
            self.kinds[key] = kind

    def load_schemas(self, schemas_dir: str) -> int:
        for dirpath, dirnames, filenames in os.walk(schemas_dir):
            if os.path.basename(dirpath) == "abstract":
                dirnames[:] = []
                continue
            for fn in filenames:
                if is_spec_file(fn):
                    path = os.path.join(dirpath, fn)
                    try:
                        self.add_schema(path, load_file(path))
                    except Exception:  # noqa: BLE001 - same tolerance as the indexer
                        continue
        return len(self.kinds)

    # ----------------------------------------------------------- listings

    def services(self) -> list[str]:
        return sorted({o.service for o in self.operations})

    def list_operations(self, service: str = "", contains: str = "") -> str:
        ops = self.operations
        if service:
            needle = service.strip().lower()
            ops = [o for o in ops if needle in o.service.lower()]
            if not ops:
                return f"No service '{service}'. Services: {', '.join(self.services())}."
        if contains:
            needle = contains.strip().lower()
            ops = [o for o in ops if needle in f"{o.path} {o.summary}".lower()]
            if not ops:
                return f"No operations match '{contains}'."
        lines = [f"# OSDU API operations ({len(ops)})"]
        current = None
        for o in sorted(ops, key=lambda o: (o.service, o.path, o.method)):
            if o.service != current:
                current = o.service
                lines.append(f"\n## {current}")
            summary = first_sentence(o.summary, 90)
            lines.append(f"- {o.method} {o.path}" + (f" — {summary}" if summary else ""))
        return "\n".join(lines)

    def groups(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for k in self.kinds.values():
            label = k.group or NO_GROUP
            counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items()))

    def list_kinds(self, group: str = "", contains: str = "", include_reference_data: bool = False) -> str:
        kinds = list(self.kinds.values())
        group = group.strip().lower()
        if group:
            kinds = [k for k in kinds if (k.group or NO_GROUP) == group]
            if not kinds:
                listed = ", ".join(f"{g} ({n})" for g, n in self.groups().items())
                return f"No group '{group}'. Groups: {listed}."
        elif not include_reference_data:
            kinds = [k for k in kinds if k.group != REFERENCE_GROUP]
        if contains:
            needle = contains.strip().lower()
            kinds = [k for k in kinds if needle in f"{k.name} {k.description}".lower()]
            if not kinds:
                return f"No kinds match '{contains}'."

        lines = [f"# OSDU kinds ({len(kinds)}, latest version of each)"]
        current = None
        for k in sorted(kinds, key=lambda k: (k.group, k.name.lower())):
            if k.group != current:
                current = k.group
                lines.append(f"\n## {current or NO_GROUP}")
            desc = first_sentence(k.description, 110)
            lines.append(f"- {k.short}" + (f" — {desc}" if desc else ""))
        hidden = self.groups().get(REFERENCE_GROUP, 0)
        if not group and not include_reference_data and hidden:
            lines.append(
                f"\n_{hidden} reference-data kinds (code lists such as units, CRS and type "
                "values) are hidden. Pass group=\"reference-data\" with `contains` to find one._"
            )
        return "\n".join(lines)

    def find_kind(self, kind: str) -> Kind | list[str]:
        """Look a kind up by full id, 'group--Name' or bare name. Returns the
        Kind, or a list of suggestions when there is no single match."""
        key = kind.strip().lower()
        m = _KIND_RE.match(key)
        if m:
            key = f"{m['group']}--{m['name']}" if m["group"] else m["name"]
        if key in self.kinds:
            return self.kinds[key]
        bare = [k for k in self.kinds.values() if k.name.lower() == key]
        if len(bare) == 1:
            return bare[0]
        if bare:
            return sorted(k.short for k in bare)
        return sorted(k.short for k in self.kinds.values() if key and key in k.short.lower())[:15]

    def _targets(self, prop: dict) -> list[str]:
        out = []
        for r in _relationships(prop):
            group, entity = r.get("GroupType", ""), r.get("EntityType", "")
            if entity:
                key = f"{group}--{entity}" if group else entity
                out.append(self.kinds[key.lower()].short if key.lower() in self.kinds else key)
            elif group:
                out.append(f"any {group} record")
        return list(dict.fromkeys(out))

    def properties(self, kind: Kind) -> list[Prop]:
        doc = load_file(kind.path)
        resolved = self._resolver.resolve(
            doc, base_dir=os.path.dirname(os.path.abspath(kind.path)), root=doc
        )
        if not isinstance(resolved, dict):
            resolved = doc
        # Most kinds wrap their model in "data"; content kinds have it at the root.
        data = resolved.get("properties", {}).get("data")
        schema, prefix = (data, "data") if isinstance(data, dict) else (resolved, "")
        props: dict[str, Prop] = {}
        for path, p, alt in walk_properties(schema, prefix):
            if path in props:  # the same path from several oneOf branches
                props[path].alternative &= alt
                continue
            props[path] = Prop(
                path=path,
                type=str(p.get("type", "object")),
                description=(p.get("description") or "").strip(),
                targets=self._targets(p),
                unit=_frame_of_reference(p),
                alternative=alt,
            )
        return list(props.values())

    def list_properties(self, kind: str, contains: str = "") -> str:
        found = self.find_kind(kind)
        if not isinstance(found, Kind):
            if not found:
                return f"No kind '{kind}'. Use list_kinds to see the available kinds."
            listed = "\n".join(f"  - {s}" for s in found)
            return f"No single kind matches '{kind}'. Did you mean:\n{listed}"

        props = self.properties(found)
        if contains:
            needle = contains.strip().lower()
            props = [p for p in props if needle in f"{p.path} {p.description}".lower()]

        lines = [f"# Properties of {found.kind_id} ({len(props)})"]
        if any(p.alternative for p in props):
            lines.append("_`one of` marks properties in a oneOf/anyOf branch: present in only some records._")
        for p in props:
            extra = []
            if p.targets:
                extra.append("→ " + ", ".join(p.targets))
            if p.unit:
                extra.append(f"unit: {p.unit}")
            if p.alternative:
                extra.append("one of")
            desc = first_sentence(p.description, 80)
            tail = f" [{'; '.join(extra)}]" if extra else ""
            lines.append(f"- {p.path} ({p.type})" + (f" — {desc}" if desc else "") + tail)
        if not props:
            lines.append(f"\nNo properties match '{contains}'." if contains else "\nNo data properties.")
        return "\n".join(lines)
