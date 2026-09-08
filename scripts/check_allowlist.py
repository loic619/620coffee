#!/usr/bin/env python3
"""Fail if a published payload contains anything the allow-list does not permit.

fetch/publish_ice.py prunes before writing, so in normal operation this finds
nothing. It exists because "the code that writes it also decides what is safe"
is not a control. This reads what is actually on disk, about to be committed to
a public repository, and checks it independently.

Run in CI on every push and PR, and in the fetch workflow before the commit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetch"))

from allowlist import SPECS, prune  # noqa: E402

# Never acceptable in a published file, whatever else changes. Belt to the
# allow-list's braces: these are the specific engine-derived structures the
# migration exists to keep private, named so a regression is unmistakable.
FORBIDDEN_KEYS = {
    "port_peaks": "engine-maintained ratchet over accumulated history",
    "port_origin_history": "accumulated history — 619's, not 620's",
    "implied_outflow": "cohort_outflow.py derivation",
    "current_by_origin": "cohort-DNA derivation",
}

MAX_SNAPSHOTS = 60


def walk_keys(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}{key}", item
            yield from walk_keys(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value[:200]:          # a sample is enough; rows are uniform
            yield from walk_keys(item, f"{path}[].")


def main() -> int:
    problems: list[str] = []
    checked = 0

    for market, spec in SPECS.items():
        path = ROOT / "data" / "ice" / f"{market}_latest.json"
        if not path.is_file():
            print(f"[allowlist] {market}: not published yet — skipping")
            continue
        checked += 1
        doc = json.loads(path.read_text())

        # 1. Nothing outside the allow-list survived.
        _kept, dropped = prune(doc, spec)
        for field in dropped:
            problems.append(f"{path.name}: '{field}' is not in the allow-list")

        # 2. None of the named private structures appears at any depth.
        for key_path, _ in walk_keys(doc):
            leaf = key_path.rsplit(".", 1)[-1]
            if leaf in FORBIDDEN_KEYS:
                problems.append(
                    f"{path.name}: '{key_path}' is private — {FORBIDDEN_KEYS[leaf]}")

        # 3. It is a window, not a history.
        count = len(doc.get("snapshots") or [])
        if count > MAX_SNAPSHOTS:
            problems.append(
                f"{path.name}: {count} snapshots exceeds the {MAX_SNAPSHOTS} window "
                f"rail — 620 publishes a fetch window, not history")

    if problems:
        print(f"allow-list check FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"allow-list OK — {checked} payload(s) contain only permitted raw fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
