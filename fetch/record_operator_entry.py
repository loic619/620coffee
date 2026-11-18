#!/usr/bin/env python3
"""Record what an operator knows about a missed ICE robusta session.

The robusta stock CSV is served under a filename stamped with the second it was
generated (`Stock_Report_RC_YYYYMMDD_HHMMSS.csv`), with no index. The sweep
covers a deliberately narrow window, so a late release is an announced miss —
and the only thing that recovers it is a person reading the six digits off the
ICE stock-reports page. This is where that person's answer lands.

Two answers, two files, both in 620's own committed state:

  * **the second** -> `stock_report_hits.json`, the log the fetcher reads.
    Tier 0 of `pull_stock_report` then fetches that session in one GET, at any
    age (retention, probe 0.18) — on the first run whose window reaches it.
  * **no release at all** -> `no_release_days.json`. ICE Futures Europe was
    shut (2026-08-31, UK summer bank holiday); no second will ever close the
    day, so it is filed with its reason instead of left pending forever.

619 used to own this (its record_observation.py, dispatched from the admin
research page). That path was retired with 619's fetchers in #1009, because a
second recorded in 619 lands in a log 620 never reads. 619's admin page now
dispatches the workflow that runs this file, and 619's sync brings both files
home.

Rules, each one a way the record could go wrong:

  * Robusta only. Arabica's daily report is `coffee_cert_stock_YYYYMMDD.xls` —
    no publish second in the URL, so there is nothing to type; a missed arabica
    day is refetched by widening the fetch window.
  * A date is in at most one of the two files: a second we can name is proof
    the market was open.
  * A no-release for a day whose second is known is REFUSED, not applied —
    overwriting an observation with a claim it never happened destroys data.
  * A second for a day already marked no-release withdraws the mark. Evidence
    beats classification.
  * A different second for a day that already has one is REFUSED. The recorded
    one came from a 200 response (or an earlier entry); replacing it would also
    make 619's sync refuse the date as contested. Same second = no-op.
  * Weekends, future dates and seconds outside 09:00-15:00 UTC are typos.

The hit log is written byte-for-byte the way orchestrate's own recorder writes
it (`json.dumps(..., indent=2)`, no trailing newline), so an operator entry
never rewrites the rest of the file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

ICE_DIR = (Path(__file__).resolve().parent / "scraper" / "sources" / "ice_certified_stocks")
HITS = ICE_DIR / "stock_report_hits.json"
NO_RELEASE = ICE_DIR / "no_release_days.json"

MAX_REASON = 200
# ICE has never published outside this range (observed 10:29:56 -> 12:47:15);
# anything beyond it is a typo that would poison tier 0 for that date.
EARLIEST_S = 9 * 3600
LATEST_S = 15 * 3600
# The widest window a single fetch run may publish: the weekly `--days 30`
# catch-up. scripts/check_payload_window.py refuses anything wider (75-day
# horizon = days*2+7), so a day older than this is recorded but not refetched.
MAX_FETCH_DAYS = 30
MIN_FETCH_DAYS = 3

_NO_RELEASE_NOTE = (
    "Business days on which ICE Futures Europe published no robusta "
    "certified-stock report at all — exchange holidays and closures. These are "
    "not misses: no publish second exists to find, so they are excluded from "
    "the session count rather than left pending forever."
)


class EntryRefused(ValueError):
    """An entry that must not be written. The message is shown to the operator."""


# ── validation ───────────────────────────────────────────────────────────────

def validate_market(value: str) -> str:
    m = (value or "").strip().lower()
    if m == "arabica":
        raise EntryRefused(
            "arabica has no publish second to record — its daily report is "
            "coffee_cert_stock_YYYYMMDD.xls, one fixed URL per day. To refetch a "
            "missed arabica day, dispatch 'fetch ICE certified stocks' with a "
            "`days` window that reaches it.")
    if m != "robusta":
        raise EntryRefused(f"unknown market {value!r} — want robusta")
    return m


def validate_date(value: str, today: dt.date | None = None) -> str:
    d = (value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise EntryRefused(f"bad date {value!r} — want YYYY-MM-DD")
    try:
        day = dt.date.fromisoformat(d)
    except ValueError as exc:
        raise EntryRefused(f"not a calendar date: {value!r}") from exc
    if day > (today or dt.datetime.now(dt.UTC).date()):
        raise EntryRefused(f"{d} is in the future")
    if day.weekday() >= 5:
        raise EntryRefused(f"{d} is a {day.strftime('%A')} — ICE does not publish at weekends")
    return d


def validate_hhmmss(value: str) -> str:
    t = (value or "").strip()
    if not re.fullmatch(r"\d{6}", t):
        raise EntryRefused(f"bad time {value!r} — six digits as in the filename, e.g. 112351")
    hh, mm, ss = int(t[:2]), int(t[2:4]), int(t[4:6])
    if mm > 59 or ss > 59:
        raise EntryRefused(f"{t} is not a wall-clock time")
    if not EARLIEST_S <= hh * 3600 + mm * 60 + ss <= LATEST_S:
        raise EntryRefused(f"{t} is outside 09:00-15:00 UTC — ICE has never published there")
    return t


def validate_reason(value: str) -> str:
    r = " ".join((value or "").split())
    if not r:
        raise EntryRefused("a reason is required — it is the only record of why the day is closed")
    if len(r) > MAX_REASON:
        raise EntryRefused(f"reason is {len(r)} characters; keep it under {MAX_REASON}")
    return r


# ── file I/O ─────────────────────────────────────────────────────────────────

def _load(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return json.loads(json.dumps(default))


def _save_hits(path: Path, doc: dict) -> None:
    # orchestrate._record_stock_report_hit's exact format.
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _save_no_release(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _hit_for(doc: dict, date: str) -> dict | None:
    for h in doc.get("hits", []):
        if h.get("date") == date and h.get("hhmmss"):
            return h
    return None


# ── the two entries ──────────────────────────────────────────────────────────

def record_time(date: str, hhmmss: str, *, hits_path: Path = HITS,
                no_release_path: Path = NO_RELEASE, today: dt.date | None = None) -> str:
    date, hhmmss = validate_date(date, today), validate_hhmmss(hhmmss)
    hits = _load(hits_path, {"hits": []})
    existing = _hit_for(hits, date)
    if existing and existing["hhmmss"] != hhmmss:
        raise EntryRefused(
            f"{date} already has publish second {existing['hhmmss']} on record — "
            f"refusing to replace it with {hhmmss}. If the recorded one is wrong, "
            "that is a hand edit with a reason, not an operator entry.")
    msg = (f"{date} already recorded at {hhmmss} — nothing to change" if existing
           else f"recorded {date} -> {hhmmss}")
    if not existing:
        # Appended, like orchestrate's recorder: tier 1 counts frequency, not
        # order, and the cap there trims from the head.
        hits.setdefault("hits", []).append({"date": date, "hhmmss": hhmmss, "source": "operator"})
        _save_hits(hits_path, hits)

    nr = _load(no_release_path, {"note": _NO_RELEASE_NOTE, "days": []})
    if any(d.get("date") == date for d in nr.get("days", [])):
        nr["days"] = [d for d in nr["days"] if d.get("date") != date]
        _save_no_release(no_release_path, nr)
        msg += " (withdrew its 'no release' mark — the report exists)"
    return msg


def record_no_release(date: str, reason: str, *, hits_path: Path = HITS,
                      no_release_path: Path = NO_RELEASE, today: dt.date | None = None,
                      at: str | None = None) -> str:
    date, reason = validate_date(date, today), validate_reason(reason)
    existing = _hit_for(_load(hits_path, {"hits": []}), date)
    if existing:
        raise EntryRefused(
            f"{date} already has a recorded publish second ({existing['hhmmss']}) — "
            "it did release. Nothing to pass.")
    doc = _load(no_release_path, {"note": _NO_RELEASE_NOTE, "days": []})
    doc.setdefault("note", _NO_RELEASE_NOTE)
    days = [d for d in doc.get("days", []) if d.get("date") != date]
    days.append({
        "date": date,
        "reason": reason,
        "source": "operator",
        "recorded": at or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    days.sort(key=lambda d: d.get("date", ""))
    doc["days"] = days
    _save_no_release(no_release_path, doc)
    return f"marked {date} as no release — {reason}"


def fetch_days_for(date: str, now_utc: dt.datetime | None = None) -> int | None:
    """The `days` window a fetch run needs for its tier 0 to reach `date`.

    Mirrors publish_ice.window_days: the anchor is the prior business day, or
    today from 18:00 UTC. Returns None when the day is older than the widest
    window a run may publish — recorded, but no single run will refetch it."""
    now_utc = now_utc or dt.datetime.now(dt.UTC)
    anchor = now_utc.date() if now_utc.hour >= 18 else now_utc.date() - dt.timedelta(days=1)
    day = dt.date.fromisoformat(date)
    n, cur = 0, anchor
    while cur >= day:
        if cur.weekday() < 5:
            n += 1
        cur -= dt.timedelta(days=1)
    n = max(n, MIN_FETCH_DAYS)
    return n if n <= MAX_FETCH_DAYS else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--market", default="robusta")
    ap.add_argument("--date", required=True, help="session date, YYYY-MM-DD")
    ap.add_argument("--hhmmss", default="", help="publish second from the filename")
    ap.add_argument("--reason", default="", help="why there was no release (instead of --hhmmss)")
    args = ap.parse_args(argv)

    out = os.environ.get("GITHUB_OUTPUT")
    try:
        validate_market(args.market)
        has_time, has_reason = bool(args.hhmmss.strip()), bool(args.reason.strip())
        if has_time == has_reason:
            raise EntryRefused("give exactly one of: a publish second, or a no-release reason")
        if has_time:
            print(record_time(args.date, args.hhmmss, hits_path=HITS, no_release_path=NO_RELEASE))
            days = fetch_days_for(args.date.strip())
            if days is None:
                print(f"::warning::{args.date} is older than the {MAX_FETCH_DAYS}-business-day "
                      "window a fetch run may publish — the second is recorded, but no run "
                      "will refetch this session.")
            if out:
                with open(out, "a", encoding="utf-8") as fh:
                    fh.write(f"fetch_days={days or ''}\n")
        else:
            print(record_no_release(args.date, args.reason, hits_path=HITS, no_release_path=NO_RELEASE))
            if out:
                with open(out, "a", encoding="utf-8") as fh:
                    fh.write("fetch_days=\n")
    except EntryRefused as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
