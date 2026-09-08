#!/usr/bin/env python3
"""Validate (and regenerate) the 620coffee dataset catalogue.

Checks that catalog.json describes exactly what is committed under data/:
every entry resolves to a file, every hash and size matches, ids are unique
and well formed, and no dataset file is published without an entry.

Usage:
    python3 scripts/validate_catalog.py            # check; non-zero exit on failure
    python3 scripts/validate_catalog.py --update   # recompute hashes/sizes, rewrite CATALOG.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog.json"
CATALOG_MD = ROOT / "CATALOG.md"
DATA = ROOT / "data"

CATALOG_VERSION = 1
ID_RE = re.compile(r"^[a-z0-9]+(\.[a-z0-9_-]+){1,3}$")
REQUIRED = ("id", "title", "source", "path", "format", "bytes", "sha256", "updated_at")
# Files allowed under data/ without a catalogue entry.
NON_DATASET = {"README.md", ".gitkeep"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load() -> dict:
    try:
        return json.loads(CATALOG.read_text())
    except FileNotFoundError:
        sys.exit("catalog.json is missing")
    except json.JSONDecodeError as exc:
        sys.exit(f"catalog.json is not valid JSON: {exc}")


def check_against_schema(catalog: dict) -> list[str]:
    """Validate the catalogue against schemas/catalog.schema.json.

    jsonschema is optional: the structural checks below stand on their own, so a
    machine without it still gets a meaningful result. CI installs it.
    """
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return []
    schema = json.loads((ROOT / "schemas" / "catalog.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    return [
        "schema: " + "/".join(str(p) for p in err.absolute_path) + f": {err.message}"
        for err in sorted(validator.iter_errors(catalog), key=lambda e: list(e.absolute_path))
    ]


def check(catalog: dict) -> list[str]:
    errors: list[str] = check_against_schema(catalog)

    if catalog.get("catalog_version") != CATALOG_VERSION:
        errors.append(
            f"catalog_version is {catalog.get('catalog_version')!r}, expected {CATALOG_VERSION}"
        )
    datasets = catalog.get("datasets")
    if not isinstance(datasets, list):
        return errors + ["catalog.json has no 'datasets' list"]

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()

    for entry in datasets:
        ident = entry.get("id", "<no id>")
        missing = [f for f in REQUIRED if f not in entry]
        if missing:
            errors.append(f"{ident}: missing required field(s): {', '.join(missing)}")
            continue

        if not ID_RE.match(entry["id"]):
            errors.append(f"{ident}: id must look like <source>.<family>[.<variant>]")
        if entry["id"] in seen_ids:
            errors.append(f"{ident}: duplicate id")
        seen_ids.add(entry["id"])

        rel = entry["path"]
        if rel in seen_paths:
            errors.append(f"{ident}: two entries claim {rel}")
        seen_paths.add(rel)

        if not rel.startswith("data/"):
            errors.append(f"{ident}: path must live under data/, got {rel}")
            continue
        if ".." in Path(rel).parts:
            errors.append(f"{ident}: path must not escape the repository")
            continue

        expected_dir = f"data/{entry['source']}/"
        if not rel.startswith(expected_dir):
            errors.append(f"{ident}: source is {entry['source']!r} but path is {rel}")

        target = ROOT / rel
        if not target.is_file():
            errors.append(f"{ident}: {rel} does not exist")
            continue

        actual_bytes = target.stat().st_size
        if entry["bytes"] != actual_bytes:
            errors.append(f"{ident}: bytes is {entry['bytes']}, file is {actual_bytes}")
        actual_hash = sha256(target)
        if entry["sha256"] != actual_hash:
            errors.append(f"{ident}: sha256 does not match {rel}")

        if entry["format"] == "json":
            try:
                json.loads(target.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors.append(f"{ident}: {rel} is not valid JSON: {exc}")

        schema = entry.get("schema")
        if schema and not (ROOT / schema).is_file():
            errors.append(f"{ident}: schema {schema} does not exist")

    if DATA.is_dir():
        for path in sorted(DATA.rglob("*")):
            if not path.is_file() or path.name in NON_DATASET:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel not in seen_paths:
                errors.append(f"{rel} is committed under data/ but has no catalogue entry")

    return errors


def render_markdown(catalog: dict) -> str:
    datasets = sorted(catalog.get("datasets", []), key=lambda d: d.get("id", ""))
    lines = [
        "# Catalogue",
        "",
        "<!-- Generated by scripts/validate_catalog.py --update. Do not edit by hand. -->",
        "",
        f"Catalogue version {catalog.get('catalog_version')} · "
        f"{len(datasets)} dataset{'' if len(datasets) == 1 else 's'} · "
        f"generated {catalog.get('generated_at', 'n/a')}",
        "",
        "`catalog.json` is the machine-readable form of this table and is the "
        "authority; this file is rendered from it.",
        "",
    ]
    if not datasets:
        lines += [
            "No datasets are published yet. The structure, catalogue format and "
            "data contract are in place and migration is in progress — see the "
            "README.",
            "",
        ]
        return "\n".join(lines)

    by_source: dict[str, list[dict]] = {}
    for entry in datasets:
        by_source.setdefault(entry["source"], []).append(entry)

    for source in sorted(by_source):
        lines += [
            f"## `{source}`",
            "",
            "| Dataset | id | Coverage | Cadence | Size | Updated |",
            "|---|---|---|---|---|---|",
        ]
        for entry in by_source[source]:
            coverage = entry.get("coverage") or {}
            span = (
                f"{coverage.get('start', '?')} → {coverage.get('end', '?')}"
                if coverage
                else "—"
            )
            size = f"{entry['bytes'] / 1_000_000:.1f} MB" if entry["bytes"] >= 1_000_000 \
                else f"{entry['bytes'] / 1_000:.0f} kB"
            flag = " *(deprecated)*" if entry.get("deprecated") else ""
            lines.append(
                f"| [{entry['title']}{flag}]({entry['path']}) | `{entry['id']}` | "
                f"{span} | {entry.get('cadence', '—')} | {size} | "
                f"{entry['updated_at'][:10]} |"
            )
        lines.append("")
    return "\n".join(lines)


def update(catalog: dict) -> dict:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for entry in catalog.get("datasets", []):
        target = ROOT / entry.get("path", "")
        if not target.is_file():
            continue
        digest = sha256(target)
        if entry.get("sha256") != digest:
            entry["updated_at"] = now
        entry["sha256"] = digest
        entry["bytes"] = target.stat().st_size
    catalog["generated_at"] = now
    catalog["datasets"] = sorted(catalog.get("datasets", []), key=lambda d: d.get("id", ""))
    CATALOG.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    CATALOG_MD.write_text(render_markdown(catalog))
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="recompute sizes and hashes and re-render CATALOG.md before checking",
    )
    args = parser.parse_args()

    catalog = load()
    if args.update:
        catalog = update(catalog)

    errors = check(catalog)

    rendered = render_markdown(catalog)
    if not CATALOG_MD.is_file():
        errors.append("CATALOG.md is missing (run with --update)")
    elif CATALOG_MD.read_text() != rendered:
        errors.append("CATALOG.md is out of date with catalog.json (run with --update)")

    if errors:
        print(f"catalogue validation failed ({len(errors)} problem(s)):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    count = len(catalog.get("datasets", []))
    print(f"catalogue OK — {count} dataset(s) validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
