#!/usr/bin/env python3
"""Fetch ICE certified stocks and publish the current window.

620 runs 619's parsers unmodified (see PORTING.md), with `merge=False` so the
result contains only the days this run actually fetched — never accumulated
history. The result is then pruned to the raw-field allow-list and written to
data/ice/ alongside a .status sidecar.

    python fetch/publish_ice.py --days 3          # daily
    python fetch/publish_ice.py --days 30         # weekly catch-up
    python fetch/publish_ice.py --days 3 --dry-run

Nothing here accumulates. Nothing here derives. History and derivation are
619's, and stay 619's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetch"))

from allowlist import SPECS, prune  # noqa: E402

# Keys under which a dated record may appear. ICE reports use several names for
# "the day this record is about"; a record carrying none of them is kept, since
# guessing it is old would silently drop data.
DATE_KEYS = ("date", "report_date", "month_end", "month", "cut_off_date")

CATALOG = ROOT / "catalog.json"
STAGE = ROOT / "_stage"
OUT = ROOT / "data" / "ice"
MARKETS = ("certified_stocks_arabica", "certified_stocks_robusta")

# Hard rail. 620 publishes a fetch window, never a history: the widest planned
# run is the weekly --days 30 catch-up, so anything approaching a year of
# snapshots means the fetch merged against history or was misconfigured.
# Refusing to write is the safe failure — a public repo cannot un-publish.
MAX_SNAPSHOTS = int(os.environ.get("ICE_MAX_SNAPSHOTS", "60"))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _record_date(record) -> str | None:
    if not isinstance(record, dict):
        return None
    for key in DATE_KEYS:
        value = record.get(key)
        if isinstance(value, str) and len(value) >= 7:
            return value[:10]
    return None


def trim_to_window(doc: dict, cutoff: str) -> tuple[dict, dict]:
    """Drop dated records older than `cutoff` from every nested list.

    The orchestrator's own structures — recent_activity.*, monthly.* — carry
    accumulated history: a three-snapshot run was observed holding 152 tender
    records reaching back fifteen months. History is 619's, so publishing it
    would breach the architecture even though every field in it is raw.

    This is deliberately belt-and-braces with the fetch window itself. The
    fetch should already return only the window; this guarantees it.
    """
    removed: dict[str, int] = {}

    def walk(value, path=""):
        if isinstance(value, list):
            kept = []
            for item in value:
                stamp = _record_date(item)
                if stamp is not None and stamp < cutoff:
                    removed[path] = removed.get(path, 0) + 1
                    continue
                kept.append(walk(item, path))
            return kept
        if isinstance(value, dict):
            return {k: walk(v, f"{path}.{k}" if path else k) for k, v in value.items()}
        return value

    return walk(doc), removed


def _record_telemetry(orchestrate, out: dict) -> None:
    """Write ice_run_stats.json and print the request picture.

    orchestrate.py records its own telemetry from `__main__` only, and 620
    imports `run()` as a library — so nothing here was writing the stats file
    and the workflow's telemetry artifact came up empty. Observed on run
    34219640496: "No files were found with the provided path". Do what 619's
    CLI does, from this wrapper, so orchestrate.py stays byte-identical.

    Never fatal. This runs after a successful fetch and must not be the thing
    that loses it.
    """
    stats = orchestrate._RUN_STATS
    try:
        orchestrate._record_run_stats(
            outcome=("aborted_429" if stats["aborted_by_429"]
                     else "aborted_403" if stats["aborted_by_403"]
                     else "completed"),
            sweep_day=out.get("_sweep_day"),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[fetch]   run telemetry not recorded: {e}")

    # Same two lines 619 prints, so a run's economics can be read off the log
    # without downloading the artifact — and compared line for line with 619's.
    print(f"WAIT: publicdocs {stats['wait_publicdocs_s']/60:.1f} min · "
          f"marketdata {stats['wait_marketdata_s']/60:.1f} min · "
          f"retry-after {sum(stats['retry_after_waits'])/60:.1f} min "
          f"over {stats['requests']} requests")
    print(f"RATE: {stats['http_429']} x 429 · "
          f"{stats['http_403']} x 403 · "
          f"{len(stats['retry_after_waits'])} Retry-After waits "
          f"({round(sum(stats['retry_after_waits']))}s total) · "
          f"{stats['throttle_bumps']} throttle bumps · "
          f"{stats['sweep_gets']} sweep GETs · "
          f"{stats['http_404']} x 404 · "
          f"{stats['ok_200']} x 200")


def run_fetch(days: int) -> dict:
    """Run the ported orchestrator. merge=False → the fetched window only."""
    os.environ.setdefault("ICE_STAGE_DIR", str(STAGE))
    from scraper.sources.ice_certified_stocks import orchestrate

    out = orchestrate.run(days_back=days, write=True, merge=False, skip_monthly=False)
    _record_telemetry(orchestrate, out)
    return out


def publish(cutoff: str, dry_run: bool = False) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0
    published: dict[str, dict] = {}

    for market in MARKETS:
        staged = STAGE / f"{market}.json"
        status_path = OUT / f"{market}_latest.status.json"
        payload_path = OUT / f"{market}_latest.json"

        if not staged.is_file():
            print(f"[publish] {market}: nothing staged — fetch produced no file")
            status = {"dataset": f"ice.{market}", "ok": False, "fetched_at": now(),
                      "error": "fetch produced no staged output"}
            if not dry_run:
                status_path.write_text(json.dumps(status, indent=2) + "\n")
            failures += 1
            continue

        raw = json.loads(staged.read_text())
        kept, dropped = prune(raw, SPECS[market])
        kept, trimmed = trim_to_window(kept, cutoff)

        snapshots = kept.get("snapshots") or []
        dates = sorted(s["date"] for s in snapshots if isinstance(s, dict) and s.get("date"))

        body = json.dumps(kept, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
        digest = sha256_bytes(body.encode())

        status = {
            "dataset": f"ice.{market}",
            "ok": bool(snapshots),
            "fetched_at": now(),
            "payload": payload_path.name,
            "sha256": digest,
            "bytes": len(body.encode()),
            "snapshots": len(snapshots),
            "window": {"start": dates[0], "end": dates[-1]} if dates else None,
            "errors": raw.get("errors") or [],
            "dropped_fields": sorted(dropped),
            "trimmed_before": cutoff,
            "trimmed_records": trimmed,
        }

        print(f"[publish] {market}: {len(snapshots)} snapshot(s) "
              f"{status['window'] or '—'} · {len(body)/1000:.0f} kB")
        if dropped:
            print(f"[publish]   dropped (not raw): {', '.join(sorted(dropped))}")
        if trimmed:
            total = sum(trimmed.values())
            print(f"[publish]   trimmed {total} record(s) older than {cutoff}: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(trimmed.items())))
        if not snapshots:
            print("[publish]   WARNING: no snapshots — publishing status only")
            failures += 1

        if len(snapshots) > MAX_SNAPSHOTS:
            print(f"[publish]   REFUSING: {len(snapshots)} snapshots exceeds the "
                  f"{MAX_SNAPSHOTS}-snapshot window rail. 620 publishes a fetch "
                  f"window, not history. Nothing written.")
            status["ok"] = False
            status["error"] = (f"payload had {len(snapshots)} snapshots, rail is "
                               f"{MAX_SNAPSHOTS}")
            if not dry_run:
                status_path.write_text(json.dumps(status, indent=2) + "\n")
            failures += 1
            continue

        if dry_run:
            print(f"[publish]   --dry-run: would write {payload_path.name}")
            continue

        # A payload with no snapshots is not published: it would replace a good
        # window with an empty one for any consumer reading the file directly.
        # The status file still records the failed attempt.
        if snapshots:
            payload_path.write_text(body)
            published[market] = status
        status_path.write_text(json.dumps(status, indent=2) + "\n")

    if published and not dry_run:
        update_catalog(published)

    return failures


CATALOG_META = {
    "certified_stocks_arabica": ("ICE certified arabica stocks — current window", "daily"),
    "certified_stocks_robusta": ("ICE certified robusta stocks — current window", "daily"),
}


def update_catalog(entries: dict[str, dict]) -> None:
    """Refresh catalog.json for the markets this run published.

    The catalogue is a discovery index, not a mirror manifest: one entry per
    dataset currently published, carrying the hash a consumer verifies against.
    A market that failed this run keeps its previous entry — the payload on disk
    is still the last good one.
    """
    if not CATALOG.is_file():
        raise SystemExit(f"[publish] {CATALOG} is missing — refusing to create one. "
                         f"The catalogue is committed state, not a side effect.")
    catalog = json.loads(CATALOG.read_text())
    by_id = {d["id"]: d for d in catalog.get("datasets", [])}

    for market, info in entries.items():
        title, cadence = CATALOG_META[market]
        dataset_id = f"ice.{market}_latest"
        entry = by_id.get(dataset_id, {})
        entry.update({
            "id": dataset_id,
            "title": title,
            "description": ("The days most recently fetched from ICE's published "
                            "certified-stock reports. A window, not a history."),
            "source": "ice",
            "path": f"data/ice/{market}_latest.json",
            "format": "json",
            "bytes": info["bytes"],
            "sha256": info["sha256"],
            "updated_at": info["fetched_at"],
            "cadence": cadence,
            "coverage": ({"start": info["window"]["start"], "end": info["window"]["end"]}
                         if info.get("window") else None),
            "upstream": "https://www.ice.com/",
        })
        if entry["coverage"] is None:
            entry.pop("coverage")
        by_id[dataset_id] = entry

    catalog["datasets"] = sorted(by_id.values(), key=lambda d: d["id"])
    catalog["generated_at"] = now()
    CATALOG.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    print(f"[publish] catalogue updated — {len(catalog['datasets'])} dataset(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3,
                    help="business-day window to fetch (3 daily, 30 weekly catch-up)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and prune, write nothing")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="publish from an already-staged fetch (for tests)")
    args = ap.parse_args()

    # Calendar margin over the business-day window: --days 3 spans a weekend,
    # and a report can be published a day or two late.
    cutoff = (datetime.now(UTC).date() - timedelta(days=args.days * 2 + 7)).isoformat()

    if not args.skip_fetch:
        print(f"=== ICE fetch · window = {args.days} business days · merge=False ===")
        run_fetch(args.days)

    print(f"=== publish · keeping records dated {cutoff} or later ===")
    return publish(cutoff, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
