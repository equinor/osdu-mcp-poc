"""Refresh the OpenAPI specs and OSDU schemas this index is built from.

Both artifact sets in this repository are copies of files published elsewhere,
so a refresh is an overwrite, never a merge:

  specs/    OpenAPI documents each OSDU service publishes on
            community.opengroup.org. Provenance lives in spec_sources.yaml,
            which mirrors the file of the same name in osdu-csharp-client.

  schemas/  the Generated/ tree of the OSDU data-definitions repository,
            pinned to a tag. Provenance lives in schema_sources.yaml. This
            follows osdu-csharp-models, which pins the same upstream as a
            reviewable snapshot rather than tracking a moving branch.

Usage
    python refresh_artifacts.py --specs          # specs only
    python refresh_artifacts.py --schemas        # schemas only
    python refresh_artifacts.py --all            # both
    python refresh_artifacts.py --all --check    # report drift, write nothing

After a refresh the index must be rebuilt, otherwise queries still answer from
the old content:

    osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db --reset
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, timezone, datetime

import yaml
from rich.console import Console

console = Console()

# Messages here carry file paths, URLs, hashes and git output, so rich's markup
# parsing and value highlighting are both off: a stray bracket in upstream text
# would otherwise raise or silently vanish from the output. soft_wrap keeps long
# paths and URLs on one line so they stay copy-pasteable.
def say(text: str = "", style: str | None = None) -> None:
    console.print(text, style=style, markup=False, highlight=False, soft_wrap=True)


REPO = os.path.dirname(os.path.abspath(__file__))
SPECS_DIR = os.path.join(REPO, "specs")
SCHEMAS_DIR = os.path.join(REPO, "schemas")
SPEC_SOURCES = os.path.join(REPO, "spec_sources.yaml")
SCHEMA_SOURCES = os.path.join(REPO, "schema_sources.yaml")

COMMUNITY = "https://community.opengroup.org"

# service -> (gitlab project path, file path within that project)
#
# Unit Service v2 is deliberately absent: every one of its operations is marked
# deprecated upstream, so only v3 is indexed.
SPECS: dict[str, tuple[str, str]] = {
    "CRS_Catalog": (
        "osdu/platform/system/reference/crs-catalog-service",
        "docs/api/community/v3/openapi.yaml",
    ),
    "CRS_Conversion": (
        "osdu/platform/system/reference/crs-conversion-service",
        "docs/api/community/v4/openapi.yaml",
    ),
    "Dataset": (
        "osdu/platform/system/dataset",
        "docs/api/community/v1/openapi.yaml",
    ),
    "Entitlements": (
        "osdu/platform/security-and-compliance/entitlements",
        "docs/api/community/v2/openapi.yaml",
    ),
    "File": (
        "osdu/platform/system/file",
        "docs/api/community/v2/openapi.yaml",
    ),
    "Geospatial": (
        "osdu/platform/consumption/geospatial",
        "docs/api/community/v3/openapi.yaml",
    ),
    "Indexer": (
        "osdu/platform/system/indexer-service",
        "docs/api/community/v2/openapi.yaml",
    ),
    "Legal": (
        "osdu/platform/security-and-compliance/legal",
        "docs/api/community/v1/openapi.yaml",
    ),
    "Notification": (
        "osdu/platform/system/notification",
        "docs/api/community/v1/openapi.yaml",
    ),
    "Partition": (
        "osdu/platform/system/partition",
        "docs/api/community/v1/openapi.yaml",
    ),
    "Policy": (
        "osdu/platform/security-and-compliance/policy",
        "docs/api/community/v1/openapi.json",
    ),
    "Register": (
        "osdu/platform/system/register",
        "docs/api/community/v1/openapi.yaml",
    ),
    "Schema": (
        "osdu/platform/system/schema-service",
        "docs/api/community/v1/openapi.yaml",
    ),
    "Search": (
        "osdu/platform/system/search-service",
        "docs/api/community/v2/openapi.yaml",
    ),
    # Seismic is the one service that does not publish under docs/api/community;
    # it ships the spec inside the service tree.
    "Seismic_ddms": (
        "osdu/platform/domain-data-mgmt-services/seismic/seismic-dms-suite/seismic-store-service",
        "app/sdms/docs/api/openapi.yaml",
    ),
    "Storage": (
        "osdu/platform/system/storage",
        "docs/api/community/v2/openapi.yaml",
    ),
    "Unit": (
        "osdu/platform/system/reference/unit-service",
        "docs/api/community/v3/openapi.yaml",
    ),
    "Wellbore_ddms": (
        "osdu/platform/domain-data-mgmt-services/wellbore/wellbore-domain-services",
        "docs/api/community/v1/openapi.json",
    ),
    "Workflow": (
        "osdu/platform/data-flow/ingestion/ingestion-workflow",
        "docs/api/community/v1/openapi.yaml",
    ),
}

SPEC_REF = "master"

# The schema snapshot. data-definitions tag v0.30.0 is the M27.0 milestone
# publication -- the same snapshot osdu-csharp-models pins as schemas/M27.0.
SCHEMA_PROJECT = "osdu/data/data-definitions"
SCHEMA_TAG = "v0.30.0"
SCHEMA_SNAPSHOT = "M27.0"
SCHEMA_SUBDIR = "Generated"

CONNECT_TIMEOUT = 30
READ_TIMEOUT = 120

# The extensions the indexer accepts in specs/. Anything here is indexed, so
# anything here that we did not put there is drift.
SPEC_EXTENSIONS = {".yaml", ".yml", ".json"}


def raw_url(project: str, path: str, ref: str) -> str:
    return f"{COMMUNITY}/{project}/-/raw/{ref}/{path}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


def fetch(url: str) -> bytes:
    """Download a file, tolerating a TLS-intercepting corporate proxy.

    Python trusts its own CA bundle, which on a machine behind TLS inspection
    does not contain the proxy's root. curl trusts the system store, which the
    machine's management does populate, so it succeeds where Python cannot.
    Falling back keeps this runnable both on such machines and in CI, without
    disabling verification and without a new dependency.

    Both paths are bounded: an unresponsive proxy should fail the refresh, not
    park it forever with no output.
    """
    try:
        with urllib.request.urlopen(url, timeout=READ_TIMEOUT) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} for {url}")
            return resp.read()
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        proc = subprocess.run(
            [
                "curl", "--silent", "--show-error", "--fail", "--location",
                "--connect-timeout", str(CONNECT_TIMEOUT),
                "--max-time", str(READ_TIMEOUT),
                url,
            ],
            capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"curl fallback failed for {url}: {proc.stderr.decode(errors='replace').strip()}"
            ) from exc
        return proc.stdout


def parse_spec(data: bytes) -> dict:
    """Decode a download and confirm it really is an OpenAPI document.

    A captive portal, an SSO redirect or a GitLab error page is served with
    status 200, so a successful response proves nothing. Hashing those bytes
    blind would write the page into specs/ and record it in spec_sources.yaml
    as `state: identical` -- provenance asserting the service's spec is current
    when it holds an HTML error. The indexer would then drop or misreport that
    service. Parsing here turns that into a loud failure instead.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"not valid UTF-8 ({exc})") from exc

    try:
        # YAML is a superset of JSON, so this covers both formats upstream uses.
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        raise RuntimeError(f"does not parse as YAML or JSON: {detail}") from exc

    if not isinstance(doc, dict):
        kind = type(doc).__name__
        raise RuntimeError(f"parsed as {kind}, not a mapping - probably an error page")
    if not (doc.get("openapi") or doc.get("swagger")):
        raise RuntimeError("no openapi/swagger key - not an OpenAPI document")
    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise RuntimeError("no paths - nothing for the indexer to read")
    return doc


def spec_filename(service: str, data: bytes) -> str:
    """Keep this repo's flat `<Service>.<ext>` layout, with the real format.

    The indexer reads `specs/` non-recursively and derives the service label
    from the filename stem, so the directory stays flat and the extension has
    to reflect what the file actually is. A couple of services publish minified
    JSON at a path ending in `.yaml`, so the content decides the extension, not
    the URL. YAML is a superset of JSON and the indexer would parse either, but
    a `.yaml` file holding JSON is a trap for anyone reading the tree.
    """
    return service + (".json" if data.lstrip()[:1] == b"{" else ".yaml")


def refresh_specs(check_only: bool) -> tuple[int, int, int, list[dict]]:
    os.makedirs(SPECS_DIR, exist_ok=True)

    # Phase 1 -- fetch and validate every service before touching the tree.
    #
    # Writing as we went meant a failure on service 12 left services 1-11
    # replaced on disk while main() bailed out before rewriting
    # spec_sources.yaml, i.e. new specs described by old provenance. Staging
    # first makes the refresh all-or-nothing.
    planned: list[dict] = []
    failed = 0

    for service in sorted(SPECS):
        project, path = SPECS[service]
        url = raw_url(project, path, SPEC_REF)

        try:
            data = fetch(url)
            parse_spec(data)
        except Exception as exc:  # noqa: BLE001 - collected, then aborts the run
            say(f"  FAIL  {service}: {exc}", style="red")
            failed += 1
            continue

        planned.append(
            {
                "service": service,
                "project": project,
                "path": path,
                "url": url,
                "spec": spec_filename(service, data),
                "data": data,
                "sha": sha256_bytes(data),
            }
        )

    if failed:
        return 0, failed, 0, []

    # Phase 2 -- reconcile the directory as a whole.
    #
    # Per-service checks only ever see stems still listed in SPECS, so a spec
    # whose service was renamed or dropped stayed behind and kept being
    # indexed while --check reported success. The indexer reads every
    # .yaml/.yml/.json in specs/, so the honest question is not "is each
    # service current" but "does this directory contain exactly the files we
    # expect" -- which also covers a copy left under a superseded extension.
    expected = {p["spec"] for p in planned}
    present = {
        name
        for name in os.listdir(SPECS_DIR)
        if os.path.isfile(os.path.join(SPECS_DIR, name))
        and os.path.splitext(name)[1].lower() in SPEC_EXTENSIONS
    }
    extra = sorted(present - expected)

    entries: list[dict] = []
    changed = 0

    for p in planned:
        target = os.path.join(SPECS_DIR, p["spec"])
        local_hash = sha256_file(target)

        if local_hash == p["sha"]:
            status = "ok"
        else:
            status = "stale" if local_hash else "new"
            changed += 1
            if not check_only:
                with open(target, "wb") as fh:
                    fh.write(p["data"])
                local_hash = p["sha"]

        say(f"  {status:5s} {p['service']:16s} {len(p['data']):>9,d} B  {p['spec']}")
        entries.append(
            {
                "service": p["service"],
                "spec": p["spec"],
                "project": p["project"],
                "path": p["path"],
                "ref": SPEC_REF,
                "url": p["url"],
                "state": "identical" if local_hash == p["sha"] else "differs",
                "upstream_sha256": p["sha"],
                "local_sha256": local_hash,
                "verified": date.today().isoformat(),
            }
        )

    for name in extra:
        stem = os.path.splitext(name)[0]
        shadowed = next((p["spec"] for p in planned if p["service"] == stem), None)
        reason = (
            f"shadows {shadowed}" if shadowed
            else "no longer published by any indexed service"
        )
        if check_only:
            say(f"  extra {name} ({reason})", style="yellow")
        else:
            os.remove(os.path.join(SPECS_DIR, name))
            say(f"  removed {name} ({reason})", style="yellow")

    return changed, failed, len(extra), entries


def write_spec_sources(entries: list[dict]) -> None:
    """Rewrite the `specs:` block, preserving the explanatory header."""
    with open(SPEC_SOURCES, "r", encoding="utf-8") as fh:
        header = fh.read().split("\nspecs:")[0].rstrip("\n")

    header = "\n".join(
        f'verified: "{date.today().isoformat()}"' if line.startswith("verified:") else line
        for line in header.split("\n")
    )

    lines = [header, "", "specs:"]
    for e in entries:
        lines.append(f"  - service: {e['service']}")
        lines.append(f"    spec: {e['spec']}")
        lines.append(f"    project: {e['project']}")
        lines.append(f"    path: {e['path']}")
        lines.append(f"    ref: {e['ref']}")
        lines.append(f"    url: {e['url']}")
        lines.append(f"    state: {e['state']}")
        lines.append(f"    upstream_sha256: {e['upstream_sha256']}")
        lines.append(f"    local_sha256: {e['local_sha256']}")
        lines.append(f'    verified: "{e["verified"]}"')
        lines.append("")
    with open(SPEC_SOURCES, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")


def schema_checkout() -> tuple[str, str | None]:
    """Return a path to data-definitions at SCHEMA_TAG, and a temp dir to clean.

    Prefers a sibling checkout (the layout osdu-csharp-models documents) when it
    already sits on the pinned tag; otherwise clones the tag shallowly.
    """
    sibling = os.path.join(os.path.dirname(REPO), "data-definitions")
    if os.path.isdir(os.path.join(sibling, ".git")):
        try:
            described = subprocess.run(
                ["git", "-C", sibling, "describe", "--tags", "--exact-match"],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            if described == SCHEMA_TAG:
                # An exact tag says nothing about the worktree: `git describe`
                # still reports it when tracked files under Generated/ are
                # edited or untracked files are sitting there. Copying those in
                # would record the official tag and commit as provenance for
                # bytes that are not upstream, so only reuse the sibling when
                # that subtree is clean. --porcelain covers untracked too.
                status = subprocess.run(
                    ["git", "-C", sibling, "status", "--porcelain", "--", SCHEMA_SUBDIR],
                    capture_output=True, text=True, check=False,
                )
                # A failed status is not a clean one. When git refuses the
                # repository -- dubious ownership is the common case -- it
                # writes the complaint to stderr and leaves stdout empty, which
                # reads identically to "no changes". Reuse demands an explicit
                # success, so an unverifiable checkout falls through to a clone
                # rather than silently passing as clean.
                if status.returncode != 0:
                    detail = status.stderr.strip().splitlines()
                    say(
                        f"  sibling checkout could not be verified "
                        f"({detail[0] if detail else f'git status exited {status.returncode}'}), "
                        f"cloning {SCHEMA_TAG} instead"
                    )
                elif not status.stdout.strip():
                    say(f"  using sibling checkout at {SCHEMA_TAG}: {sibling}")
                    return sibling, None
                else:
                    n = len(status.stdout.strip().splitlines())
                    say(
                        f"  sibling checkout is at {SCHEMA_TAG} but has {n} local "
                        f"change(s) under {SCHEMA_SUBDIR}/, cloning {SCHEMA_TAG} instead"
                    )
            else:
                say(f"  sibling checkout is at {described or 'an untagged commit'}, cloning {SCHEMA_TAG}")
        except OSError:
            pass

    tmp = tempfile.mkdtemp(prefix="data-definitions-")
    url = f"{COMMUNITY}/{SCHEMA_PROJECT}.git"
    say(f"  cloning {SCHEMA_TAG} from {url}")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", SCHEMA_TAG, url, tmp],
        check=True, capture_output=True,
    )
    return tmp, tmp


def refresh_schemas(check_only: bool) -> tuple[int, int, int, dict]:
    source_root, tmp = schema_checkout()
    try:
        generated = os.path.join(source_root, SCHEMA_SUBDIR)
        if not os.path.isdir(generated):
            raise RuntimeError(f"{generated} not found")

        rev = subprocess.run(
            ["git", "-C", source_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        # Recording an empty commit would leave schema_sources.yaml claiming a
        # snapshot it cannot identify, so treat this as fatal rather than
        # writing unusable provenance.
        if rev.returncode != 0 or not rev.stdout.strip():
            raise RuntimeError(
                f"could not resolve HEAD in {source_root}: "
                f"{rev.stderr.strip() or f'git rev-parse exited {rev.returncode}'}"
            )
        commit = rev.stdout.strip()

        added = updated = removed = 0
        upstream_files: set[str] = set()

        for root, _dirs, files in os.walk(generated):
            for name in files:
                if not name.endswith(".json"):
                    continue
                src = os.path.join(root, name)
                rel = os.path.relpath(src, generated)
                upstream_files.add(rel)
                dst = os.path.join(SCHEMAS_DIR, rel)

                src_hash = sha256_file(src)
                dst_hash = sha256_file(dst)
                if dst_hash is None:
                    added += 1
                elif dst_hash != src_hash:
                    updated += 1
                else:
                    continue

                if not check_only:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)

        # Anything no longer published upstream is dropped, so the index cannot
        # keep answering from a schema that has been withdrawn.
        for root, _dirs, files in os.walk(SCHEMAS_DIR):
            for name in files:
                if not name.endswith(".json"):
                    continue
                rel = os.path.relpath(os.path.join(root, name), SCHEMAS_DIR)
                if rel not in upstream_files:
                    removed += 1
                    if not check_only:
                        os.remove(os.path.join(SCHEMAS_DIR, rel))
                        say(f"        removed withdrawn {rel}")

        total = len(upstream_files)
        meta = {
            "snapshot": SCHEMA_SNAPSHOT,
            "project": SCHEMA_PROJECT,
            "tag": SCHEMA_TAG,
            "commit": commit,
            "subdir": SCHEMA_SUBDIR,
            "files": total,
            "verified": date.today().isoformat(),
        }
        return added, updated, removed, meta
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def write_schema_sources(meta: dict) -> None:
    content = f"""# Upstream provenance for the schema tree in schemas/.
#
# schemas/ is a pinned copy of the OSDU data-definitions `{meta['subdir']}/` tree --
# the fully resolved schemas the platform publishes, not the authoring sources.
# It is pinned to a tag rather than tracked against a branch, so the content
# behind the index only moves in an explicit, reviewable change. This mirrors
# osdu-csharp-models, which pins the same upstream as schemas/{meta['snapshot']}/.
#
# Refresh with:
#
#     python refresh_artifacts.py --schemas
#
# Bumping the snapshot means editing SCHEMA_TAG and SCHEMA_SNAPSHOT in
# refresh_artifacts.py, re-running it, and rebuilding the index.

version: 1

snapshot: {meta['snapshot']}
project: {meta['project']}
tag: {meta['tag']}
commit: {meta['commit']}
subdir: {meta['subdir']}
files: {meta['files']}
verified: "{meta['verified']}"
"""
    with open(SCHEMA_SOURCES, "w", encoding="utf-8") as fh:
        fh.write(content)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--specs", action="store_true", help="refresh specs/")
    ap.add_argument("--schemas", action="store_true", help="refresh schemas/")
    ap.add_argument("--all", action="store_true", help="refresh both")
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    args = ap.parse_args()

    do_specs = args.specs or args.all
    do_schemas = args.schemas or args.all
    if not (do_specs or do_schemas):
        ap.error("pass --specs, --schemas or --all")

    drift = 0

    if do_specs:
        say(f"OpenAPI specs ({len(SPECS)} services, ref {SPEC_REF})", style="bold")
        changed, failed, extra, entries = refresh_specs(args.check)
        if failed:
            say(f"  {failed} service(s) could not be fetched - nothing was written", style="red")
            return 1
        if not args.check:
            write_spec_sources(entries)
            say(f"  wrote {os.path.basename(SPEC_SOURCES)}")
        summary = f"  {changed} changed, {len(entries) - changed} already current"
        if extra:
            summary += f", {extra} unexpected file(s) {'found' if args.check else 'removed'}"
        say(summary + "\n")
        drift += changed + extra

    if do_schemas:
        say(f"OSDU schemas (data-definitions {SCHEMA_TAG}, snapshot {SCHEMA_SNAPSHOT})", style="bold")
        added, updated, removed, meta = refresh_schemas(args.check)
        if not args.check:
            write_schema_sources(meta)
            say(f"  wrote {os.path.basename(SCHEMA_SOURCES)}")
        say(f"  {added} added, {updated} updated, {removed} removed, {meta['files']} total\n")
        drift += added + updated + removed

    if args.check:
        say(
            f"drift: {drift} artifact(s) differ from upstream",
            style="yellow" if drift else "green",
        )
        return 1 if drift else 0

    say("Rebuild the index so queries see the new content:")
    say("  osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db --reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
