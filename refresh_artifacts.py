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
    """
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} for {url}")
            return resp.read()
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        proc = subprocess.run(
            ["curl", "--silent", "--show-error", "--fail", "--location", url],
            capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"curl fallback failed for {url}: {proc.stderr.decode(errors='replace').strip()}"
            ) from exc
        return proc.stdout


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


def refresh_specs(check_only: bool) -> tuple[int, int, list[dict]]:
    os.makedirs(SPECS_DIR, exist_ok=True)
    entries: list[dict] = []
    changed = failed = 0

    for service in sorted(SPECS):
        project, path = SPECS[service]
        url = raw_url(project, path, SPEC_REF)

        try:
            data = fetch(url)
        except Exception as exc:  # noqa: BLE001 - reported per service, not fatal
            print(f"  FAIL  {service}: {exc}")
            failed += 1
            continue

        target_name = spec_filename(service, data)
        target = os.path.join(SPECS_DIR, target_name)

        upstream_hash = sha256_bytes(data)
        local_hash = sha256_file(target)

        if local_hash == upstream_hash:
            status = "ok"
        else:
            status = "stale" if local_hash else "new"
            changed += 1
            if not check_only:
                # Drop copies of the same service under a different extension,
                # so a format change upstream cannot leave the service indexed
                # twice from two files.
                for stale in (service + ".yaml", service + ".yml", service + ".json"):
                    if stale != target_name:
                        stale_path = os.path.join(SPECS_DIR, stale)
                        if os.path.exists(stale_path):
                            os.remove(stale_path)
                            print(f"        removed stale {stale}")
                with open(target, "wb") as fh:
                    fh.write(data)
                local_hash = upstream_hash

        print(f"  {status:5s} {service:16s} {len(data):>9,d} B  {target_name}")
        entries.append(
            {
                "service": service,
                "spec": target_name,
                "project": project,
                "path": path,
                "ref": SPEC_REF,
                "url": url,
                "state": "identical" if local_hash == upstream_hash else "differs",
                "upstream_sha256": upstream_hash,
                "local_sha256": local_hash,
                "verified": date.today().isoformat(),
            }
        )

    return changed, failed, entries


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
                print(f"  using sibling checkout at {SCHEMA_TAG}: {sibling}")
                return sibling, None
            print(f"  sibling checkout is at {described or 'an untagged commit'}, cloning {SCHEMA_TAG}")
        except OSError:
            pass

    tmp = tempfile.mkdtemp(prefix="data-definitions-")
    url = f"{COMMUNITY}/{SCHEMA_PROJECT}.git"
    print(f"  cloning {SCHEMA_TAG} from {url}")
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

        commit = subprocess.run(
            ["git", "-C", source_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()

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
                        print(f"        removed withdrawn {rel}")

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
        print(f"OpenAPI specs ({len(SPECS)} services, ref {SPEC_REF})")
        changed, failed, entries = refresh_specs(args.check)
        if failed:
            print(f"  {failed} service(s) could not be fetched")
            return 1
        if not args.check:
            write_spec_sources(entries)
            print(f"  wrote {os.path.basename(SPEC_SOURCES)}")
        print(f"  {changed} changed, {len(entries) - changed} already current\n")
        drift += changed

    if do_schemas:
        print(f"OSDU schemas (data-definitions {SCHEMA_TAG}, snapshot {SCHEMA_SNAPSHOT})")
        added, updated, removed, meta = refresh_schemas(args.check)
        if not args.check:
            write_schema_sources(meta)
            print(f"  wrote {os.path.basename(SCHEMA_SOURCES)}")
        print(f"  {added} added, {updated} updated, {removed} removed, {meta['files']} total\n")
        drift += added + updated + removed

    if args.check:
        print(f"drift: {drift} artifact(s) differ from upstream")
        return 1 if drift else 0

    print("Rebuild the index so queries see the new content:")
    print("  osdu-index --specs ./specs --schemas ./schemas --db ./chroma_db --reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
