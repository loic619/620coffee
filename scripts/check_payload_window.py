#!/usr/bin/env python3
"""Assert that a published payload contains only records inside its window.

This exists because of a specific, real regression. The ICE parsers accumulate
internally: `recent_activity.tenders`, `.gradings`, `.iss_recv_daily`,
`.grading_overview` and `monthly.age_allowance` keep every record they have ever
seen. A nominal three-day run was measured carrying 425 such records, the oldest
from 2025-06-25 — fifteen months of history inside a three-day payload, every
field of it raw and so invisible to the allow-list.

publish_ice.py trims them. This checks the trim actually happened, reading the
files on disk rather than trusting the code that wrote them. It walks EVERY
nested array, not a list of known sections, so a new report family added
upstream is covered the day it appears rather than the day someone remembers to
add it here.

Two independent assertions, because either alone can be defeated:

  1. The window the payload DECLARES must be plausible — no wider than
     MAX_HORIZON_DAYS. A bug that declared a five-year cutoff would otherwise
     make assertion 2 vacuous.
  2. No dated record anywhere in the payload may fall before that declared
     window.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# The widest acquisition run is the weekly `--days 30` catch-up, whose cutoff is
# days*2+7 = 67 calendar days. Anything beyond that is not a window.
MAX_HORIZON_DAYS = 75

DATE_KEYS = ("date", "report_date", "month_end", "month", "cut_off_date")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def record_date(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    for key in DATE_KEYS:
        value = record.get(key)
        if isinstance(value, str) and ISO_DATE.match(value):
            return value[:10]
    return None


def dated_records(value: object, path: str = ""):
    """Every dated record anywhere in the document, with the path it sits at."""
    if isinstance(value, list):
        for item in value:
            stamp = record_date(item)
            if stamp is not None:
                yield path, stamp
            yield from dated_records(item, path)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from dated_records(item, f"{path}.{key}" if path else key)


def check_payload(payload_path: Path, status_path: Path, today: date) -> list[str]:
    problems: list[str] = []
    name = payload_path.name

    if not status_path.is_file():
        return [f"{name}: no .status.json beside it — the declared window is unknown"]

    status = json.loads(status_path.read_text())
    declared = status.get("trimmed_before")
    if not declared or not ISO_DATE.match(str(declared)):
        return [f"{name}: status declares no usable 'trimmed_before' window"]

    # 1. Is the declared window plausible?
    horizon = (today - date.fromisoformat(declared)).days
    if horizon > MAX_HORIZON_DAYS:
        problems.append(
            f"{name}: declares a {horizon}-day window ('trimmed_before': {declared}); "
            f"the widest permitted acquisition window is {MAX_HORIZON_DAYS} days. "
            f"A payload is a window, not a history.")
    if horizon < 0:
        problems.append(f"{name}: declares a window starting in the future ({declared})")

    # 2. Does every record actually sit inside it?
    payload = json.loads(payload_path.read_text())
    outside: dict[str, list[str]] = {}
    for path, stamp in dated_records(payload):
        if stamp < declared:
            outside.setdefault(path or "(root)", []).append(stamp)

    for path, stamps in sorted(outside.items()):
        stamps.sort()
        problems.append(
            f"{name}: {len(stamps)} record(s) under '{path}' predate the declared "
            f"window {declared} (oldest {stamps[0]}) — accumulated history must not "
            f"be published")

    if not problems:
        total = sum(1 for _ in dated_records(payload))
        print(f"[window] {name}: {total} dated record(s), all within {declared} "
              f"({horizon}-day window)")
    return problems


def main() -> int:
    today = datetime.now(UTC).date()
    problems: list[str] = []
    checked = 0

    for payload_path in sorted(DATA.rglob("*_latest.json")):
        if payload_path.name.endswith(".status.json"):
            continue
        checked += 1
        status_path = payload_path.with_name(
            payload_path.name.replace(".json", ".status.json"))
        problems.extend(check_payload(payload_path, status_path, today))

    if problems:
        print(f"payload-window check FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    if checked == 0:
        print("[window] no payloads published yet — nothing to check")
    else:
        print(f"payload-window OK — {checked} payload(s) contain only in-window records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
