#!/usr/bin/env python3
"""Fetch ICE certified stocks and publish the current window.

620 runs 619's parsers unmodified (see PORTING.md), with `merge=False` so the
result contains only the days this run actually fetched — never accumulated
history. The result is then pruned to the raw-field allow-list and written to
data/ice/ alongside a .status sidecar.

    python fetch/publish_ice.py --days 3          # daily, both markets
    python fetch/publish_ice.py --days 30         # weekly catch-up
    python fetch/publish_ice.py --days 3 --dry-run
    python fetch/publish_ice.py --days 3 --market arabica     # one pass only
    python fetch/publish_ice.py --days 3 --market robusta --budget-min 125

The fetch runs as two passes, ARABICA FIRST, each publishing its own market as
soon as it is fetched. The robusta pass carries a wall-clock budget: its tier-2
sweep stops cleanly when the budget says so, and the pass still publishes what
it has. Run 44052146050 (2026-11-17) is why: one late robusta release walked
the whole 1,810-candidate sweep, the job hit its 150-minute timeout, and the
cancel discarded arabica's three good snapshots along with everything else.

Nothing here accumulates. Nothing here derives. History and derivation are
619's, and stay 619's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
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


# ── Passes, one market each ──────────────────────────────────────────────────
#
# orchestrate.run() fetches both markets and writes both at the very end, so a
# run that dies anywhere in the robusta half loses arabica too. orchestrate.py
# stays byte-identical to 619 (PORTING.md), so the split is done from here: the
# same run() is called once per market, with the OTHER market's pull functions
# replaced by no-request stubs for the duration of the call. run() looks those
# functions up as module globals at call time, so swapping the attribute is
# enough, and every one is restored on the way out.

PASSES = ("arabica", "robusta")
MARKET_OF_PASS = {"arabica": "certified_stocks_arabica", "robusta": "certified_stocks_robusta"}

# Every network-touching pull run() makes, by the market it belongs to.
_ARABICA_PULLS = ("pull_arabica_xls", "pull_arabica_ageing")
_ROBUSTA_DAILY_PULLS = ("pull_gradings", "pull_grading_appeals", "pull_iss_recv_daily",
                        "pull_tenders", "pull_grading_overview", "pull_infested_warrant")
_ROBUSTA_MONTHLY_PULLS = ("pull_iss_recv_monthly", "pull_age_allowance")
_ROBUSTA_PULLS = ("pull_stock_report",) + _ROBUSTA_DAILY_PULLS + _ROBUSTA_MONTHLY_PULLS
# The two that return a list of (url, parsed); every other pull returns a pair.
_LIST_PULLS = frozenset({"pull_gradings", "pull_grading_appeals"})


def _empty_result(name: str):
    return [] if name in _LIST_PULLS else (None, None)


def _stub(name: str):
    def stub(*_args, **_kwargs):
        return _empty_result(name)
    stub.__name__ = f"{name}__stubbed"
    return stub


@contextmanager
def _patched(module, **attrs):
    """Set module attributes for the duration of a block, then restore them."""
    saved = {k: getattr(module, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(module, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


# ── The robusta budget ───────────────────────────────────────────────────────
#
# Wall-clock, measured from the start of the robusta pass. Two deadlines:
#
#   sweep deadline  total − reserve. Past it, the tier-2 sweep stops at its next
#                   candidate and no further stock-report search is started.
#   hard deadline   total. Past it, per-day and monthly robusta pulls return
#                   empty without a request, so run() reaches its write.
#
# The reserve is what has to happen AFTER the sweep: the per-day sources for
# every window day (620's stage is fresh each run, so every window day is a
# "gap" to orchestrate) plus the monthly walk-back. Measured shape: a day is
# ~8 GETs at the 8 s marketdata throttle (~64 s); the monthly walk-back is up to
# 30 GETs. Rounded up so a slow day still fits.
PER_DAY_RESERVE_S = 80
MONTHLY_RESERVE_S = 300
DEFAULT_ROBUSTA_BUDGET_MIN = float(os.environ.get("ICE_ROBUSTA_BUDGET_MIN", "125"))


class SweepBudgetExhausted(Exception):
    """Raised inside the tier-2 sweep when the sweep deadline has passed.

    Never escapes the pull_stock_report wrapper. It is an exception rather than
    a generator that simply stops, because a sweep loop that runs out of
    candidates sets `sweep_exhausted` and announces a LATE RELEASE — a claim
    about where ICE put the file, which a budget stop does not support.
    """


class Budget:
    def __init__(self, total_s: float, reserve_s: float, clock=time.monotonic):
        self.clock = clock
        self.start = clock()
        self.total_s = total_s
        self.reserve_s = min(reserve_s, total_s)
        self.sweep_deadline = self.start + total_s - self.reserve_s
        self.hard_deadline = self.start + total_s
        self.sweep_cut: dict | None = None
        self.skipped: Counter = Counter()

    def sweep_over(self) -> bool:
        return self.clock() >= self.sweep_deadline

    def hard_over(self) -> bool:
        return self.clock() >= self.hard_deadline

    def elapsed_min(self) -> float:
        return (self.clock() - self.start) / 60

    def summary(self) -> dict:
        return {
            "total_min": round(self.total_s / 60, 1),
            "reserve_min": round(self.reserve_s / 60, 1),
            "elapsed_min": round(self.elapsed_min(), 1),
            "sweep_cut": self.sweep_cut,
            "skipped_pulls": dict(sorted(self.skipped.items())),
        }


def reserve_for(days: int) -> float:
    return days * PER_DAY_RESERVE_S + MONTHLY_RESERVE_S


def _budgeted_sweep_times(original, budget: Budget):
    def sweep_times():
        candidates = original()

        def walk():
            for i, hhmmss in enumerate(candidates):
                if budget.sweep_over():
                    raise SweepBudgetExhausted(hhmmss, i, len(candidates))
                yield hhmmss
        return walk()
    return sweep_times


def _budgeted_stock_report(original, budget: Budget):
    def pull_stock_report(d, *, sweep=True):
        if budget.sweep_over():
            budget.skipped["pull_stock_report"] += 1
            return None, None
        try:
            return original(d, sweep=sweep)
        except SweepBudgetExhausted as stop:
            at, index, total = stop.args
            budget.sweep_cut = {"day": d.isoformat(), "stopped_before": at,
                                "candidates_walked": index, "candidates_total": total}
            print(f"::warning::[budget] tier-2 sweep for {d} stopped at {at} "
                  f"({index}/{total} candidates, {budget.elapsed_min():.0f} min into the "
                  f"robusta pass) — not found inside the budget. This is a budget stop, "
                  f"not a LATE RELEASE; later runs retry {d} through tiers 0 and 1.",
                  flush=True)
            return None, None
    return pull_stock_report


def _budgeted_pull(name: str, original, budget: Budget):
    def pull(*args, **kwargs):
        if budget.hard_over():
            if not budget.skipped[name]:
                print(f"::warning::[budget] hard deadline reached — {name} skipped "
                      f"from here on", flush=True)
            budget.skipped[name] += 1
            return _empty_result(name)
        return original(*args, **kwargs)
    return pull


# ── Holes 620 cannot publish ─────────────────────────────────────────────────
#
# orchestrate treats every hit-log date missing from its stored robusta JSON as
# a "hole" and recovers up to RECOVER_MAX of them per run, then fetches the
# per-day sources for every one it recovers. In 619 that JSON is the full
# history, so a hole is a real gap. In 620 the stage starts EMPTY every run, so
# every date in the hit log is a "hole": run 44052146050 listed 111 of them,
# re-fetched the 20 oldest (June), pulled their per-day sources, and then
# trim_to_window() dropped every one of those records before publishing.
#
# History is 619's. Dates before this run's window are therefore reported to
# orchestrate as already held, so it neither searches for them nor drags their
# per-day sources along. Dates inside the window are left alone — on the weekly
# --days 30 run, recovering them is the only way their stock reports arrive.

def window_days(orchestrate, days: int, now_utc: datetime | None = None) -> list[date]:
    """The business days orchestrate.run(days_back=days) will cover.

    Mirrors run()'s anchor rule exactly: prior business day, or today once it is
    18:00 UTC or later."""
    now_utc = now_utc or datetime.now(UTC)
    anchor = now_utc.date() if now_utc.hour >= 18 else now_utc.date() - timedelta(days=1)
    while anchor.weekday() >= 5:
        anchor -= timedelta(days=1)
    return sorted(orchestrate._biz_days_back(anchor, days))


def held_upstream_dates(orchestrate, window_start: date) -> list[str]:
    """Hit-log dates older than the window: 619's to hold, not 620's to fetch."""
    start = window_start.isoformat()
    return sorted({h["date"] for h in orchestrate._load_stock_report_hits()
                   if h.get("date") not in ("bootstrap", None) and h["date"] < start})


def _holding(original, robusta_path: Path, held: list[str]):
    held_snaps = [{"date": iso} for iso in held]

    def load_existing_json(path):
        doc = original(path)
        if not held or Path(path) != robusta_path:
            return doc
        doc = dict(doc or {})
        doc["snapshots"] = list(doc.get("snapshots") or []) + held_snaps
        return doc
    return load_existing_json


def run_pass(which: str, days: int, *, budget: Budget | None = None) -> dict:
    """Run orchestrate.run() for ONE market, staging into _stage/<which>/.

    merge=False → the fetched window only. Each pass has its own stage directory
    so the stubbed half of one pass can never be read back by the other (the
    robusta JSON's daily_fetched ledger in particular would tell the robusta
    pass that window days had already been fetched).
    """
    if which not in PASSES:
        raise ValueError(f"unknown pass {which!r}")
    os.environ.setdefault("ICE_STAGE_DIR", str(STAGE))
    from scraper.sources.ice_certified_stocks import orchestrate

    stage = STAGE / which
    stage.mkdir(parents=True, exist_ok=True)
    attrs: dict = {"OUT_DIR": stage}

    if which == "arabica":
        attrs.update({name: _stub(name) for name in _ROBUSTA_PULLS})
        # Nothing in this pass may search for a robusta day, so the hit log is
        # hidden too — otherwise run() announces 100+ "holes" it then stubs.
        attrs["_load_stock_report_hits"] = lambda: []
        print(f"=== pass 1/2 · arabica · {days} business days "
              f"(robusta pulls stubbed, no requests) ===", flush=True)
    else:
        attrs.update({name: _stub(name) for name in _ARABICA_PULLS})
        # The arabica pass has already published the ageing report it found.
        attrs["_have_ageing_for"] = lambda _month: True
        days_in_window = window_days(orchestrate, days)
        held = held_upstream_dates(orchestrate, days_in_window[0])
        if held:
            print(f"[620] {len(held)} hit-log day(s) before the window start "
                  f"{days_in_window[0]} treated as held: 620 publishes a window and "
                  f"619 holds history, so they are not re-fetched "
                  f"({held[0]} → {held[-1]})", flush=True)
        attrs["_load_existing_json"] = _holding(
            orchestrate._load_existing_json, stage / "certified_stocks_robusta.json", held)
        if budget is not None:
            attrs["_stock_report_sweep_times"] = _budgeted_sweep_times(
                orchestrate._stock_report_sweep_times, budget)
            attrs["pull_stock_report"] = _budgeted_stock_report(orchestrate.pull_stock_report, budget)
            for name in _ROBUSTA_DAILY_PULLS + _ROBUSTA_MONTHLY_PULLS:
                attrs[name] = _budgeted_pull(name, getattr(orchestrate, name), budget)
            print(f"=== pass 2/2 · robusta · {days} business days · budget "
                  f"{budget.total_s/60:.0f} min (sweep stops at "
                  f"{(budget.total_s - budget.reserve_s)/60:.0f} min) ===", flush=True)
        else:
            print(f"=== pass 2/2 · robusta · {days} business days · no budget ===", flush=True)

    with _patched(orchestrate, **attrs):
        out = orchestrate.run(days_back=days, write=True, merge=False, skip_monthly=False)
    _record_telemetry(orchestrate, out)
    if budget is not None:
        out["_budget"] = budget.summary()
        print(f"[budget] robusta pass used {budget.elapsed_min():.1f} of "
              f"{budget.total_s/60:.0f} min · {json.dumps(out['_budget'])}", flush=True)
    return out


def fetch_and_publish(passes: tuple[str, ...], days: int, cutoff: str, *,
                      budget_min: float = DEFAULT_ROBUSTA_BUDGET_MIN,
                      dry_run: bool = False, skip_fetch: bool = False) -> int:
    """Run each pass and publish its market BEFORE the next pass starts.

    Arabica goes first: it is a handful of GETs and never searches, so its data
    is on disk (and, in the workflow, committed and pushed) before the robusta
    sweep can spend any time at all."""
    failures = 0
    for which in sorted(passes, key=PASSES.index):
        notes = None
        if not skip_fetch:
            budget = (Budget(budget_min * 60, reserve_for(days))
                      if which == "robusta" else None)
            out = run_pass(which, days, budget=budget)
            if budget is not None:
                notes = {"budget": out["_budget"]}
        print(f"=== publish {which} · keeping records dated {cutoff} or later ===", flush=True)
        failures += publish(cutoff, dry_run=dry_run, markets=(MARKET_OF_PASS[which],),
                            stage=STAGE / which, notes=notes)
    return failures


def publish(cutoff: str, dry_run: bool = False, *, markets: tuple[str, ...] = MARKETS,
            stage: Path | None = None, notes: dict | None = None) -> int:
    """Publish `markets` from `stage` (default: STAGE). `notes`, if given, is
    recorded in each status sidecar — the robusta pass puts its budget there."""
    OUT.mkdir(parents=True, exist_ok=True)
    stage = stage if stage is not None else STAGE
    failures = 0
    published: dict[str, dict] = {}

    for market in markets:
        staged = stage / f"{market}.json"
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
        if notes:
            status.update(notes)

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
    ap.add_argument("--market", choices=("both",) + PASSES, default="both",
                    help="which pass to run; 'both' runs arabica, publishes it, then robusta")
    ap.add_argument("--budget-min", type=float, default=DEFAULT_ROBUSTA_BUDGET_MIN,
                    help="wall-clock budget for the robusta pass, in minutes")
    args = ap.parse_args()

    # A cancelled run's log is all there is to diagnose it, and a block-buffered
    # stdout delivers it in bursts or not at all (run 44052146050's timestamps
    # arrived in two bursts, at 09:13 and 11:24). The workflow also runs -u.
    sys.stdout.reconfigure(line_buffering=True)

    # Calendar margin over the business-day window: --days 3 spans a weekend,
    # and a report can be published a day or two late.
    cutoff = (datetime.now(UTC).date() - timedelta(days=args.days * 2 + 7)).isoformat()
    passes = PASSES if args.market == "both" else (args.market,)

    print(f"=== ICE fetch · window = {args.days} business days · merge=False · "
          f"passes = {', '.join(passes)} ===")
    return fetch_and_publish(passes, args.days, cutoff, budget_min=args.budget_min,
                             dry_run=args.dry_run, skip_fetch=args.skip_fetch)


if __name__ == "__main__":
    raise SystemExit(main())
