"""
OSDU relationship graph built from the LinkML Logical Data Model.

Reads the consolidated, latest-version-only LinkML schema (``osdu_latest.yaml``
from the OSDU schema work-products repo, ``LogicalDataModels/``) and derives:

- **reference edges** between record-bearing entities: every attribute that
  carries a ``relationship_targets`` annotation, rolled up from the abstract
  mixins and nested (synthesised) classes it lives in to the entity that
  owns it, with the JSON path it has in an OSDU record;
- **deprecations**: attributes flagged ``deprecated`` and, where known, their
  ``deprecated_element_has_exact_replacement`` successor;
- **unit quantities** per attribute (``uom_quantity`` annotation).

No RDF store is needed: the YAML is ~4 MB and parses in a few seconds.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

import yaml

try:  # libyaml is ~10x faster on a file this size
    _Loader = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader

# Group types whose classes are records in their own right (have a kind).
ENTITY_GROUPS = (
    "master-data",
    "reference-data",
    "work-product-component",
    "work-product",
    "dataset",
    "content",
)
_SYSTEM_MIXIN = "abstract/AbstractSystemProperties"
_VERSION_RE = re.compile(r"\.(\d+)\.(\d+)\.(\d+)$")
_SEE_ALSO_RE = re.compile(r"/json/([^/]+)/(.+)\.(\d+\.\d+\.\d+)\.json$")
_MAX_DEPTH = 8
# A mixin/nested class reached by at least this many entities is "common".
COMMON_THRESHOLD = 30
# Wildcard targets followed when searching paths. "Any dataset" is how every
# work-product-component links to its files (``data.Datasets[]``), so it is
# worth following; "any record" / "any master-data" / "any WPC" (ParentProjectID,
# PriorActivityIDs, ...) would connect everything to everything.
PATH_WILDCARDS = {"dataset/*"}
# Entities referenced by at least this many entity kinds count as hubs.
HUB_THRESHOLD = 75


def _annotation(attr: dict, name: str) -> str | None:
    ann = attr.get("annotations") or {}
    val = ann.get(name)
    if isinstance(val, dict):  # LinkML long form {tag:, value:}
        val = val.get("value")
    return None if val is None else str(val)


def _ranges(attr: dict) -> list[str]:
    """``range`` plus every ``any_of`` alternative (e.g. GeoContexts[] is any
    of the political / basin / field / play / prospect contexts)."""
    out = [attr["range"]] if attr.get("range") else []
    for alt in attr.get("any_of") or []:
        if isinstance(alt, dict) and alt.get("range"):
            out.append(alt["range"])
    return out


@dataclass(frozen=True)
class Edge:
    """``source`` record references ``target`` record via JSON ``path``."""

    source: str  # class key, e.g. "master-data/Wellbore"
    path: str  # e.g. "data.WellID" or "data.VerticalMeasurements[].RigID"
    target: str  # class key, e.g. "master-data/Well"
    deprecated: bool = False
    origin: str = ""  # class that defines the attribute (entity, abstract or nested)
    common: bool = False  # inherited from a mixin shared by many entities
    # The id sits in an array (the attribute or one of its ancestors is
    # multivalued), so one record can hold several different targets here.
    repeated: bool = False


@dataclass
class Deprecation:
    entity: str
    path: str
    replacement: str | None
    note: str


@dataclass
class OsduGraph:
    classes: dict[str, dict]
    entities: dict[str, str] = field(default_factory=dict)  # key -> version
    out_edges: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    in_edges: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    deprecations: dict[str, list[Deprecation]] = field(default_factory=lambda: defaultdict(list))
    units: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))
    common_classes: set[str] = field(default_factory=set)
    hubs: set[str] = field(default_factory=set)
    _names: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    # ------------------------------------------------------------------ build

    @classmethod
    def load(cls, model_path: str) -> "OsduGraph":
        with open(model_path, encoding="utf-8") as f:
            doc = yaml.load(f, Loader=_Loader)
        g = cls(classes=doc.get("classes") or {})
        g._build()
        return g

    def _build(self) -> None:
        for key, cdef in self.classes.items():
            group, _, name = key.partition("/")
            if group not in ENTITY_GROUPS or not name:
                continue
            m = _SEE_ALSO_RE.search(" ".join(cdef.get("see_also") or []))
            self.entities[key] = m.group(3) if m else ""
            self._names[name.lower()].append(key)
            self._names[f"{group}--{name}".lower()].append(key)
            self._names[key.lower()].append(key)

        raw: list[tuple] = []
        for entity in self.entities:
            seen: set[tuple[str, str]] = set()
            for attr_name, attr, prefix, dep_ctx, origin, chain in self._walk(entity, "", 0, frozenset(), ()):
                path = f"{prefix}{attr_name}"
                if (path, attr.get("range", "")) in seen:
                    continue
                seen.add((path, attr.get("range", "")))
                deprecated = dep_ctx or bool(attr.get("deprecated"))
                if attr.get("deprecated"):
                    self.deprecations[entity].append(
                        Deprecation(
                            entity,
                            path,
                            attr.get("deprecated_element_has_exact_replacement"),
                            " ".join(str(attr["deprecated"]).split()),
                        )
                    )
                quantity = _annotation(attr, "uom_quantity")
                if quantity:
                    self.units[entity].append((path, quantity))
                repeated = "[]" in path or bool(attr.get("multivalued"))
                for target in self._targets(attr):
                    raw.append((entity, path, target, deprecated, origin, repeated, chain))
        # An edge is "common" when it reaches the entity through a class that
        # most entities share (AbstractCommonResources, AbstractMaster,
        # AbstractWPCGroupType, TechnicalAssurances, NameAliases, the vertical
        # measurement reference ...). Those
        # edges are real but say nothing about the domain, and they would make
        # every entity two hops from Organisation.
        users: dict[str, set[str]] = defaultdict(set)
        for entity, *_rest, chain in raw:
            for c in chain:
                users[c].add(entity)
        shared = {c for c, us in users.items() if len(us) >= COMMON_THRESHOLD}
        self.common_classes = shared
        for entity, path, target, deprecated, origin, repeated, chain in raw:
            common = not self.is_wildcard(target) and any(c in shared for c in chain)
            edge = Edge(entity, path, target, deprecated, origin, common, repeated)
            self.out_edges[entity].append(edge)
            self.in_edges[target].append(edge)
        # Hubs: non-reference entities referenced (by domain edges) from a
        # large share of all entity kinds. Useful facts, poor path steps.
        for key in self.entities:
            if key.startswith("reference-data/"):
                continue
            sources = {
                e.source
                for e in self.in_edges.get(key, [])
                if not e.common and not e.source.startswith("reference-data/")
            }
            if len(sources) >= HUB_THRESHOLD:
                self.hubs.add(key)

    def _walk(self, key: str, prefix: str, depth: int, stack: frozenset, chain: tuple):
        """Yield (attr_name, attr, path_prefix, inherited_deprecated) for every
        attribute reachable from ``key`` through mixins / is_a (same path
        level) and nested non-entity ranges (one path level deeper)."""
        if depth > _MAX_DEPTH or key in stack:
            return
        cdef = self.classes.get(key)
        if not cdef:
            return
        stack = stack | {key}
        if depth or key not in self.entities:
            chain = chain + (key,)
        is_root = depth == 0 and key in self.entities
        # Records wrap their payload in ``data``; content records do not.
        data = "" if key.startswith("content/") else "data."
        parents = list(cdef.get("mixins") or [])
        if cdef.get("is_a"):
            parents.append(cdef["is_a"])
        for parent in parents:
            if is_root and parent == _SYSTEM_MIXIN:
                # System properties (id, kind, acl, legal, ...) sit at the top
                # level of a record, not under ``data``.
                yield from self._walk(parent, "", depth, stack, chain)
            else:
                yield from self._walk(parent, prefix or (data if is_root else ""), depth, stack, chain)
        own_prefix = prefix or (data if is_root else "")
        for attr_name, attr in (cdef.get("attributes") or {}).items():
            attr = attr or {}
            if attr_name != "oneOf":
                yield attr_name, attr, own_prefix, False, key, chain
            sep = "[]." if attr.get("multivalued") else "."
            # The generator wraps root-level JSON ``oneOf`` alternatives in a
            # synthetic ``oneOf`` attribute; it is not a path step in records.
            sub_prefix = own_prefix if attr_name == "oneOf" else f"{own_prefix}{attr_name}{sep}"
            for rng in _ranges(attr):
                if rng in self.classes and rng not in self.entities:
                    for name, a, p, dep, origin, ch in self._walk(
                        rng, sub_prefix, depth + 1, stack, chain
                    ):
                        yield name, a, p, dep or bool(attr.get("deprecated")), origin, ch

    def _targets(self, attr: dict) -> list[str]:
        raw = _annotation(attr, "relationship_targets")
        if not raw:
            return [r for r in _ranges(attr) if r in self.entities]
        out = []
        for tok in raw.split(","):
            group, _, name = tok.strip().partition("::")
            if group == "abstract":
                continue  # structural containment, not a record reference
            if name == "AnyObject" or not name:
                # Wildcard: any record of that group ("*" for any group). The
                # model spells it either ``group::AnyObject`` or ``group::``
                # (AbstractWPCGroupTypeArtefacts.ResourceID is ``dataset::``).
                if group == "any-group-type":
                    out.append("*")
                elif group in ENTITY_GROUPS:
                    out.append(f"{group}/*")
                continue
            key = f"{group}/{_VERSION_RE.sub('', name)}"
            if key in self.entities:
                out.append(key)
        return out

    # ----------------------------------------------------------------- lookup

    def resolve(self, kind: str) -> list[str]:
        """Map any common spelling of a kind to class keys.

        Accepts ``osdu:wks:master-data--Wellbore:1.5.1``,
        ``master-data--Wellbore``, ``master-data/Wellbore``, ``Wellbore``."""
        k = kind.strip()
        m = re.match(r"^[^:]+:[^:]+:([^:]+)(?::[\d.]+)?$", k)
        if m:
            k = m.group(1)
        k = _VERSION_RE.sub("", k).lower()
        hits = self._names.get(k, [])
        return sorted(set(hits))

    def kind_id(self, key: str) -> str:
        group, _, name = key.partition("/")
        ver = self.entities.get(key, "")
        return f"osdu:wks:{group}--{name}:{ver}" if ver else f"osdu:wks:{group}--{name}"

    # ------------------------------------------------------------- traversal

    @staticmethod
    def is_wildcard(key: str) -> bool:
        return key == "*" or key.endswith("/*")

    @staticmethod
    def matches(target: str, key: str) -> bool:
        """Does edge ``target`` (possibly a wildcard) cover entity ``key``?"""
        return target == key or target == "*" or target == f"{key.partition('/')[0]}/*"

    def incoming(self, key: str) -> list[Edge]:
        """Edges into ``key``, including wildcard edges that cover it."""
        group = key.partition("/")[0]
        return (
            self.in_edges.get(key, [])
            + self.in_edges.get(f"{group}/*", [])
            + self.in_edges.get("*", [])
        )

    def neighbours(
        self,
        key: str,
        include_reference_data: bool = True,
        include_common: bool = True,
        goal: str | tuple[str, ...] | None = None,
        reverse: bool = False,
    ) -> list[tuple[str, Edge, str]]:
        """(other, edge, direction) with direction 'out' (key references
        other) or 'in' (other references key). Wildcard out-edges are only
        followed when they cover ``goal`` (one key or several): expanding
        "any dataset" into every dataset kind would connect everything to
        everything.

        With ``reverse`` the result is every ``other`` from which a forward
        step onto ``key`` is allowed, rather than every ``other`` reachable
        from ``key``. The two differ because the filters are one-sided (a
        common edge may only *land on* an endpoint); find_paths uses the
        reverse form to measure distance to the goal, which must never
        overestimate a forward route."""
        res = []
        goals = (goal,) if isinstance(goal, str) else tuple(goal or ())

        def keep(other: str, e: Edge) -> bool:
            # Common-mixin edges are kept only when they land on an endpoint:
            # "Wellbore.data.GeoContexts[].FieldID -> Field" answers Wellbore->Field,
            # but as a stepping stone it would put everything two hops apart.
            # Going backwards, the landing end is ``key``, not ``other``.
            landing = key if reverse else other
            if e.common and not include_common and landing not in goals:
                return False
            return include_reference_data or other in goals or not other.startswith("reference-data/")

        for e in self.out_edges.get(key, []):
            other = e.target
            if self.is_wildcard(other):
                if other not in PATH_WILDCARDS:
                    continue
                if reverse:
                    # Forward, any kind the wildcard covers may step onto
                    # ``key`` through this edge (an incoming wildcard edge),
                    # so every one of them is a predecessor.
                    for cand in self.entities:
                        if self.matches(other, cand) and keep(cand, e):
                            res.append((cand, e, "out"))
                    continue
                hit = next((g for g in goals if self.matches(other, g)), None)
                if hit is None:
                    continue
                other = hit
            if keep(other, e):
                res.append((other, e, "out"))
        for e in self.incoming(key):
            if self.is_wildcard(e.target) and goals and e.target not in PATH_WILDCARDS:
                continue
            if keep(e.source, e):
                res.append((e.source, e, "in"))
        return res

    def find_paths(
        self,
        start: str,
        goal: str,
        max_hops: int = 4,
        extra_hops: int = 1,
        limit: int = 10,
        include_reference_data: bool = False,
        include_common: bool = False,
        include_hubs: bool = False,
    ) -> list[list[tuple[str, Edge, str]]]:
        """Entity-level paths from ``start`` to ``goal``, following references
        in either direction, shortest first. Paths up to ``extra_hops`` longer
        than the shortest are included too: the shortest link is often an
        incidental one (WellLog -> Well via a vertical-reference id) while the
        canonical route (WellLog -> Wellbore -> Well) is one hop longer.

        Reference-data entities are excluded as hops by default: nearly
        everything points at UnitOfMeasure, CoordinateReferenceSystem etc.,
        which would put every pair of entities two hops apart. Edges inherited
        from common mixins (``Edge.common``) are skipped by default for the
        same reason, and so are hub entities (``self.hubs``: Organisation,
        BusinessAgreement, ...) unless they are an endpoint. A hop that leaves
        an entity through the same slot it was entered by (X.ReportingEntityID
        can be a Wellbore *or* a Reservoir) is not a link between those two
        and is never taken."""
        if start == goal:
            return []
        kw = dict(include_reference_data=include_reference_data, include_common=include_common, goal=(start, goal))
        blocked = set() if include_hubs else (self.hubs - {start, goal})

        # Distance-to-goal by BFS backwards from the goal, used to prune the
        # DFS below. It must never overestimate: a too-large distance would
        # prune a valid route. It may underestimate (it ignores the
        # same-slot rule), which only weakens pruning; the budget loop below
        # copes with that.
        to_goal = {goal: 0}
        q = deque([goal])
        while q:
            node = q.popleft()
            if to_goal[node] >= max_hops:
                continue
            for other, _e, _d in self.neighbours(node, reverse=True, **kw):
                if other not in to_goal and other not in blocked and (
                    include_reference_data or other == start or not other.startswith("reference-data/")
                ):
                    to_goal[other] = to_goal[node] + 1
                    q.append(other)
        if start not in to_goal:
            return []

        paths: list[list[tuple[str, Edge, str]]] = []
        budget = to_goal[start]

        def dfs(node: str, acc: list, visited: set):
            if node == goal:
                paths.append(list(acc))
                return
            remaining = budget - len(acc)
            prev = acc[-1] if acc else None
            for other, edge, direction in self.neighbours(node, **kw):
                if other in visited or other in blocked or to_goal.get(other, budget + 1) > remaining - 1:
                    continue
                if (
                    prev
                    and prev[2] == "in"
                    and direction == "out"
                    and prev[1].path == edge.path
                    and not edge.repeated
                ):
                    # Entered and left via the same single-valued slot:
                    # X.ReportingEntityID is a Wellbore *or* a Reservoir,
                    # which does not relate the two. An array slot is
                    # different: one EquipmentInventory lists a RigAnchor
                    # and a RigPump side by side.
                    continue
                acc.append((other, edge, direction))
                visited.add(other)
                dfs(other, acc, visited)
                visited.discard(other)
                acc.pop()
                if len(paths) >= 5000:  # safety valve on dense neighbourhoods
                    return

        # The BFS bound can be shorter than any route the DFS accepts, so
        # widen the budget until one is found, then allow ``extra_hops`` on
        # top of that real length.
        while budget <= max_hops:
            dfs(start, [], {start})
            if paths:
                break
            budget += 1
        if not paths:
            return []
        final = min(max_hops, budget + extra_hops)
        if final > budget:
            paths.clear()
            budget = final
            dfs(start, [], {start})
        # Rank: hops, with each common-mixin link costing an extra hop
        # (WellLog->Wellbore->Well beats WellLog's vertical-reference id
        # pointing at a Well); then fewer deprecated links; then shallower
        # JSON paths, which read as more "primary".
        def rank(p):
            common = sum(e.common for _, e, _ in p)
            return (
                len(p) + common,
                common,
                sum(e.deprecated for _, e, _ in p),
                sum(e.path.count(".") for _, e, _ in p),
            )

        paths.sort(key=rank)
        unique, seen = [], set()
        for p in paths:
            sig = tuple((e.source, e.path, e.target) for _, e, _ in p)
            if sig not in seen:
                seen.add(sig)
                unique.append(p)
        return unique[:limit]
