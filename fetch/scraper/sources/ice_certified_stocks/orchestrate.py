"""Orchestrator — pulls all 10 ICE certified-stock sources and writes 2 JSONs.

Run a windowed backfill (default 30 calendar days):
    python -m scraper.sources.ice_certified_stocks.orchestrate --days 30

Or via the dedicated workflow (.github/workflows/scraper-ice-certified-stocks.yml).

Outputs:
  frontend/public/data/certified_stocks_arabica.json
  frontend/public/data/certified_stocks_robusta.json

Design notes:
  • Per-source resilience: each fetch+parse is wrapped; a single failure marks
    that source `stale_since` but does not blank the file.
  • Throttled: ~0.3 s between HTTP calls to be polite to ICE.
  • Stock report (.csv) has an HHMMSS publish timestamp in the URL — we try a
    handful of common times for *today*, but skip historical days for it (the
    other 9 sources cover history).
  • Latest day gets the full hierarchical `latest_detail`; older days collapse
    to flat `snapshots[]` rows for the time-series table.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests

from ... import run_degradations
from . import fetch as F
from .cohort_outflow import (
    build_cohort_dna,
    build_current_by_origin,
    build_implied_outflow,
    build_port_alltime_dna,
)
from .parse_age_allowance import parse_age_allowance_xlsx
from .parse_arabica_ageing import parse_arabica_ageing
from .parse_arabica_xls import parse_arabica_xls
from .parse_gradings import parse_gradings
from .parse_iss_recv import parse_iss_recv_daily, parse_iss_recv_monthly
from .parse_pdfs import parse_grading_overview_pdf, parse_infested_warrant_pdf
from .parse_stock_report import parse_stock_report
from .parse_tenders import parse_tenders

# PORTED TO 620 — 619 anchors this on the frontend's public data directory,
# which does not exist here: 620 is an acquisition worker, not an application.
# Output is staged, and fetch/publish_ice.py applies the raw-field allow-list
# before anything reaches data/ice/. Overridable so a test can use a tmpdir.
OUT_DIR = Path(os.environ.get(
    "ICE_STAGE_DIR",
    Path(__file__).resolve().parents[4] / "_stage")).resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Per-port historical extremes (latched) ──────────────────────────────────
# The warehouse gauges need a scale, and the only honest one is the port's own
# history. Deriving it from the live file alone is wrong: that file holds ~15
# months, so any port sitting at a window high reads "100% full" by
# construction — Trieste showed full on 24 lots in Aug 2026 against a real 2010
# peak of 6,169, and Antwerp showed full at 12% of its 2011 peak.
#
# The deep archives (certified_stocks_{market}_deep_YYYY-YYYY.json) carry
# per-port totals back to 2009. They are ~800 KB, far too much to ship to the
# browser just for a scale, so they are reduced here into a small `port_peaks`
# block.
#
# WHY THIS IS LATCHED, NOT RECOMPUTED. A port's all-time extreme is a fact about
# the past: it cannot change except by being exceeded. Re-deriving it from the
# archives on every run makes a permanent value depend on a fragile input — an
# archive that fails to read, gets pruned, or is missing on a fresh runner would
# silently collapse the scale back to the live window and bring the "100% full
# on 24 lots" bug straight back, with nothing in the output to show it happened.
#
# So the extremes are computed ONCE from the deepest history available, stored,
# and thereafter only ever RATCHET OUTWARD: a new high raises the max, a new low
# lowers the min, and nothing else moves them. The archives are re-read only for
# a port that has no stored value yet. Set REBUILD_PORT_PEAKS=1 to force a full
# re-derivation — needed if the archives are ever extended further back, which
# is the one case a latched value would be too narrow.

_KC_PORT_ALIASES = {
    "NOR": "NOLA", "NO": "NOLA", "MIA": "MIAMI", "MI": "MIAMI",
    "NYK": "NY", "HAM": "HA/BR", "HA": "HA/BR", "HO": "HOU", "VIR": "VA",
}


def _canonical_port(market: str, code: str) -> str:
    c = (code or "").upper()
    # Robusta codes are already canonical; arabica drifted between the workbook
    # (long forms) and older snapshots (short forms).
    return _KC_PORT_ALIASES.get(c, c) if market == "arabica" else c


def _port_series_keys(market: str) -> tuple[str, ...]:
    """Snapshot fields carrying the per-port map, live and deep shapes."""
    if market == "arabica":
        return ("by_port", "by_port_totals")
    return ("by_port_lots",)


def _observe_extremes(market: str, snapshots, acc: dict) -> dict:
    """Fold snapshots into {port: {min,max,...}}, widening what is already there."""
    keys = _port_series_keys(market)
    for s in snapshots or []:
        if not isinstance(s, dict):
            continue
        when = s.get("date")
        for k in keys:
            by_port = s.get(k)
            if not isinstance(by_port, dict):
                continue
            # A day whose report failed to publish arrives as an empty map;
            # skipping it keeps a blank day out of the minimum.
            for port, value in by_port.items():
                try:
                    v = float(value)
                except (TypeError, ValueError):
                    continue
                if v < 0:
                    continue
                p = _canonical_port(market, port)
                a = acc.get(p)
                if a is None:
                    acc[p] = {
                        "min": v, "min_date": when, "max": v, "max_date": when,
                        "first_seen": when, "last_seen": when, "observations": 1,
                    }
                    continue
                a["observations"] = (a.get("observations") or 0) + 1
                if v > a["max"]:
                    a["max"], a["max_date"] = v, when
                if v < a["min"]:
                    a["min"], a["min_date"] = v, when
                if when:
                    if not a.get("first_seen") or when < a["first_seen"]:
                        a["first_seen"] = when
                    if not a.get("last_seen") or when > a["last_seen"]:
                        a["last_seen"] = when
            break
    return acc


def _scan_deep_archives(market: str, acc: dict) -> int:
    """Fold every deep archive for `market` into `acc`. Returns files read."""
    read = 0
    for path in sorted(OUT_DIR.glob(f"certified_stocks_{market}_deep_*.json")):
        try:
            _observe_extremes(market, json.loads(path.read_text(encoding="utf-8")).get("snapshots"), acc)
            read += 1
        except Exception as e:                                   # noqa: BLE001
            # Loud, because a silently-skipped archive is what would make a
            # first computation too narrow.
            print(f"[peaks] {market}: FAILED to read {path.name}: {e}")
    return read


def build_port_peaks(market: str, live_snapshots: list[dict], previous: dict | None = None) -> dict:
    """Latched all-time min/max per port.

    `previous` is the prior run's `port_peaks`. Stored ports are carried
    forward untouched and only ratcheted outward by the live snapshots; the
    deep archives are re-read only for ports with no stored value (or when
    REBUILD_PORT_PEAKS=1 forces a full re-derivation).
    """
    rebuild = os.environ.get("REBUILD_PORT_PEAKS") == "1"
    stored: dict = {}
    if previous and not rebuild:
        for code, v in previous.items():
            if isinstance(v, dict) and v.get("max") is not None:
                stored[code] = dict(v)

    # Which ports does today's data know about that we have no locked value for?
    live_only: dict = {}
    _observe_extremes(market, live_snapshots, live_only)
    unknown = [p for p in live_only if p not in stored]

    archives_read = 0
    if rebuild or not stored or unknown:
        why = ("REBUILD_PORT_PEAKS=1" if rebuild
               else "no stored peaks" if not stored
               else f"new port(s) {', '.join(sorted(unknown))}")
        base: dict = {} if (rebuild or not stored) else stored
        archives_read = _scan_deep_archives(market, base)
        stored = base
        print(f"[peaks] {market}: re-derived from {archives_read} archives ({why})")

    # Ratchet: the only thing that moves a locked extreme is being exceeded.
    widened = []
    for code, obs in live_only.items():
        a = stored.get(code)
        if a is None:
            stored[code] = obs
            continue
        if obs["max"] > a["max"]:
            widened.append(f"{code} max {a['max']:.0f}→{obs['max']:.0f}")
            a["max"], a["max_date"] = obs["max"], obs["max_date"]
        if obs["min"] < a["min"]:
            widened.append(f"{code} min {a['min']:.0f}→{obs['min']:.0f}")
            a["min"], a["min_date"] = obs["min"], obs["min_date"]
        if obs.get("last_seen") and (not a.get("last_seen") or obs["last_seen"] > a["last_seen"]):
            a["last_seen"] = obs["last_seen"]
        if obs.get("first_seen") and (not a.get("first_seen") or obs["first_seen"] < a["first_seen"]):
            a["first_seen"] = obs["first_seen"]

    for a in stored.values():
        for f in ("min", "max"):
            if a.get(f) is not None:
                a[f] = int(round(a[f]))
        a.setdefault("locked", True)

    if widened:
        print(f"[peaks] {market}: ratcheted — {'; '.join(widened)}")
    elif not archives_read:
        print(f"[peaks] {market}: {len(stored)} ports locked, unchanged")
    return dict(sorted(stored.items(), key=lambda kv: -(kv[1].get("max") or 0)))

# Per-path throttle. Both prefixes have rate limits — discovered by the 180-day
# backfill which hit 429 on /publicdocs/ (arabica) after ~50 sequential 1 s/req
# calls. /marketdata/ is even stricter. New defaults give Akamai breathing room
# while still completing 180 days in <2 h.
TIMEOUT = 30
# 2026-09-07, at the operator's call: public 2.0 → 4.0, marketdata 5.0 → 8.0.
# The read is that both ICE paths draw on one per-IP budget, so pacing the whole
# scraper back should lower the block rate. Not measured — the 5 Sep finding was
# that 403s track the runner IP rather than request pacing — so if the blocks
# continue, these are the first values to put back.
_THROTTLE = {"public": 4.0, "marketdata": 8.0}
_THROTTLE_CAP = 15.0           # ceiling when self-bumping on 429 retries
TOO_MANY_429S = 4              # bail-out after this many consecutive 429s
# Same idea for 403, which had no bail-out at all. A missing report answers 404,
# so a 403 is anomalous — Akamai serving a challenge page instead of the file.
# Without this the 2026-09-04 block cost a full 96-minute tier-2 sweep: every
# one of the 1,920 candidates was refused, none could ever match, and the walk
# only ended when the 120-minute job timeout killed it. Eight consecutive is
# ~24 seconds of sweep, long enough to ride out a blip and short enough that a
# real block costs seconds instead of two hours.
TOO_MANY_403S = 8
# ── What the 403 is, and what this file does NOT fix ─────────────────────────
#
# Evidence, 2026-09-05, two runs of this same code on the same day:
#
#   06:31  1.13, runner A   1,796 requests   27 x 200   0 x 403
#   16:35  1.14, runner B      26 requests    0 x 200  26 x 403 (from request 1)
#
# One runner was served 1,796 times without a refusal; another was refused
# before it had asked for anything. Same headers, same hosts, same code, hours
# apart. That rules out our request volume, our pacing, our headers and the URL
# shapes — a block we had earned would have appeared partway through the
# 1,796-request run, and it did not. The refusal is `text/html` where a missing
# file answers 404, i.e. a WAF page rather than the file server.
#
# So: GitHub-hosted runner IP -> ICE/WAF -> 403. What is NOT established is the
# WAF vendor or why that IP is listed; nothing here captures enough to say, and
# _record_block_signature exists to close that gap on the next occurrence.
#
# Everything in this module is therefore about SURVIVING the block well —
# detect it in 8 requests, keep the data, stop billing, alert once, recover on
# its own. None of it stops ICE refusing a runner. The candidate fixes for that
# are all infrastructure and none belong in this file:
#
#   * a fixed egress IP (self-hosted runner, or a proxy with a stable address)
#   * asking ICE to allowlist that address
#
# Do not attempt to fix it by tuning headers, user agents or the request
# interval. The natural experiment above already says those are not the cause,
# and each attempt costs a run to disprove.
RETRY_AFTER_MAX_S = 90         # cap any Retry-After we'll wait for
RETRY_AFTER_GIVE_UP_S = 600    # if Akamai asks > this, abort the rest of the run
# `aborted` is RUN-wide and only 429 sets it: rate limiting is cumulative, so
# continuing anywhere makes it worse. `section_blocked` is the 403 equivalent
# and is deliberately NARROWER — it skips the rest of the current section and
# is cleared when the next one starts.
#
# The difference matters. A run that fetched arabica and most of robusta, then
# met a 403 storm on the last source, used to have every remaining request
# silently short-circuited by the run-wide flag: a mostly-good run quietly
# stopped collecting. A 403 is also per-source-IP and can be transient within a
# run, so one refused source is not evidence the next one is refused too.
_RATE_STATE: dict[str, int] = {
    "consecutive_429s": 0, "consecutive_403s": 0, "aborted": 0, "section_blocked": 0,
}


def _begin_section(label: str) -> None:
    """Start a fetch section with a clean 403 slate.

    Each section gets its own chance: being refused while chasing the stock
    report says nothing about whether the tenders file will serve. The cost of
    being wrong is bounded — TOO_MANY_403S requests per section — and
    _assert_not_wholly_refused still fails the run if nothing at all succeeds.
    """
    if _RATE_STATE["section_blocked"]:
        print(f"  → 403 block cleared; {label} gets a fresh attempt")
    _RATE_STATE["section_blocked"] = 0
    _RATE_STATE["consecutive_403s"] = 0
    _CURRENT_SECTION["label"] = label
    _RUN_STATS["sections"] += 1


def _record_section_block() -> None:
    """Name the section this 403 storm is about to skip.

    Called once per block, at the moment the threshold trips, so the label is
    still the section that earned it — `_begin_section` overwrites it the
    instant the next one starts. `skipped_requests` then accumulates on the
    short-circuit path below, which is what turns "we were blocked" into "we
    gave up N requests worth of this source".
    """
    _RUN_STATS["blocked_sections"].append({
        "section": _CURRENT_SECTION["label"],
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "after_403s": _RATE_STATE["consecutive_403s"],
        "skipped_requests": 0,
    })
    # The flag and the detail are one event, so one place sets both. They were
    # separate lines in _http_get, which is how a run could end up flagged
    # `aborted_403` with no record of which section it cost.
    _RUN_STATS["aborted_by_403"] = 1

# Fix 5: per-request interval (seconds) for the SEQUENTIAL tier-2 stock-report
# sweep only. Concurrency bans the IP at any level, so the per-request gap is
# the one knob left. This overrides the 5s marketdata throttle for the sweep
# (restored right after) to probe ICE's sequential-rate ceiling — step it down
# 5 → 4 → 3 → 2 → 1 → 0.5 ONLY after a run at the current value draws no 429.
# 3s came from probe 0.20 (run 32979574768, 2026-08-26): 200 sequential
# candidates at 3s drew zero 429s, zero transport failures, flat 0.13s latency,
# and a known-good control returned 200 before, four times during, and after.
# The caveat that probe could not settle is that a worst-case sweep is ~1,900
# requests, not 200 — it ruled out a short-window limit, not a daily one.
#
# Stepped back to 4.0 on 2026-09-07 at the operator's call, pacing the whole
# scraper back against a per-IP budget the 403s are charged to.
#
# THIS INTERVAL IS WHAT DECIDES WHETHER A MISS CAN BE DECLARED. The sweep must
# finish inside the job timeout, or "swept the whole window, found nothing" is
# never reachable and every unlucky day dies as a timeout instead. At 4s the
# 1,810-candidate window is 120.7 minutes, which is why the job timeout moved
# with it — see timeout-minutes in scraper-ice-certified-stocks.yml. Changing
# either number without re-checking the other breaks the late-release alert
# silently.
_STOCK_SWEEP_INTERVAL_S = 4.0

# Stock_report.csv's HHMMSS publish time varies daily. Strategy is tiered:
#   Tier 1 — try the K most-frequent HHMMSS values from past successful
#            captures (loaded from STOCK_REPORT_HITS_PATH). Cheap: ≤K GETs.
#   Tier 2 — if Tier 1 misses, sweep every second of STOCK_REPORT_SWEEP_RANGE
#            (see below for why that range is narrow on purpose). 1,920 GETs at
#            3 s/req = 96 min for a full walk, which fits the 120-min job
#            timeout — so a sweep that ends empty has genuinely looked
#            everywhere and says so, instead of being indistinguishable from a
#            run that ran out of time. Resumable via the cursor, and skipped
#            during multi-day backfills (`sweep=False`).
# Every successful capture is appended to the hits file so the Tier 1
# ordering self-tunes over time.
STOCK_REPORT_HITS_PATH = Path(__file__).with_name("stock_report_hits.json")
# Inclusive minute range to sweep: [10:29 … 11:00] = 32 minutes.
#
# Was 10:30–11:15, and all three misses in the June–August window fell outside
# what that could reach: 2026-06-10 published at 10:29:56 — four seconds before
# the window opened — and 2026-06-29 at 12:47:15, ninety-two minutes after it
# closed. The old bound was fitted to the days the sweep already succeeded on,
# which is exactly the sample that cannot show you its own blind spots.
#
# Widening this used to be unaffordable: 8,700 candidates at 4s is 9.7 hours
# against a 120-minute timeout, and a day not reached inside one run was lost.
# Retention changes that. Probe 0.18 confirmed ICE still serves reports from
# June in late August, so an unfinished sweep is a PAUSE, not a loss — the
# cursor resumes it on the next run, and the day is still there to be found.
# Deliberately narrower than the observed range. 10:29–11:00 covers 58 of the
# 60 sessions on record (97%); the two it gives up on published at 11:23 and
# 12:47, and reaching those would mean a 9-hour walk to buy two days a quarter.
# A day outside the window is now an EXPECTED, ANNOUNCED outcome rather than a
# silent hole: the run says so on Telegram, the research page lists it as
# pending, and one operator-supplied second backfills it.
# (H, M, S) inclusive on both ends. It was (H, M) until 2026-09-07, where the
# end bound implicitly meant ":59" — a convention that read as an instant and
# had already been miscounted once. Seconds are explicit now, so the window says
# exactly what it walks.
STOCK_REPORT_SWEEP_RANGE = ((10, 29, 50), (10, 59, 59))


def _hhmmss_to_s(t: tuple[int, int, int]) -> int:
    """(H, M, S) → seconds since midnight."""
    return t[0] * 3600 + t[1] * 60 + t[2]
# K = 10 (was 5) — wider Tier 1 keeps the cheap path covering more days as
# the publish window expands; only matters once the hits log fills out.
STOCK_REPORT_TIER1_K = 10
# Cap the per-run hole recovery. Each is a single GET (tier 0), but a bad
# hit-log edit should not turn one run into an unbounded backfill.
RECOVER_MAX = 20

# ── Sweep resume + run telemetry ─────────────────────────────────────────────
# Two files, both committed by the workflow so they survive across runs.
#
# RESUME: the sweep is a linear walk from 10:30. When a run is killed — the
# 120-min timeout, a cancelled queue slot, a 429 abort — the next one used to
# start again at 10:30 and re-pay every second it had already ruled out. The
# cursor records how far the walk got for a given day, so a second attempt
# resumes instead of restarting. Ruling out 10:30:00–10:45:00 is durable
# knowledge: those files do not appear later.
#
# STATS: nothing has ever recorded WHY a run was expensive. Counting 429s,
# Retry-After waits, throttle bumps and sweep GETs per run is what turns "it
# took 111 minutes" into an answerable question.
STOCK_REPORT_CURSOR_PATH = Path(__file__).with_name("stock_report_cursor.json")
RUN_STATS_PATH = Path(__file__).with_name("ice_run_stats.json")
RUN_STATS_KEEP = 200

_RUN_STATS: dict = {
    "http_429": 0, "retry_after_waits": [], "throttle_bumps": 0,
    "aborted_by_429": 0, "sweep_gets": 0, "http_404": 0, "resumed_from": None,
    # 403 is a BLOCK, not a rate limit: Akamai serving a challenge page instead
    # of the file. It needs its own counter because the remedy is the opposite
    # of a 429's — slowing down does nothing. `ok_200` is the denominator that
    # makes "everything was refused" distinguishable from "nothing was due".
    "http_403": 0, "ok_200": 0, "aborted_by_403": 0,
    # Seconds actually SLEPT between requests, split by which host's limit
    # imposed it. This is the answer to "where did the 111 minutes go" — the
    # run is almost entirely deliberate waiting, and this says whose.
    "wait_publicdocs_s": 0.0, "wait_marketdata_s": 0.0, "requests": 0,
    # True only when the sweep walked the ENTIRE window and found nothing —
    # i.e. the report published outside 10:29–11:00. Distinct from a run that
    # stopped early because it was rate-limited or ran out of clock, which
    # says nothing about where the file is.
    "sweep_exhausted": False,
    # Sections a 403 storm cut short, and how many requests each gave up.
    # `aborted_by_403` is a bare flag: it says the run met a block, not what
    # the run therefore came home WITHOUT — and those are different facts,
    # because the sections are not interchangeable. A skipped arabica xls is a
    # missing snapshot; a skipped robusta stock report is a missing session.
    # `sections` is the denominator that makes "2 of 5 refused" sayable.
    "blocked_sections": [], "sections": 0,
    # First 403's sanitised fingerprint. Every diagnosis of this block so far
    # has had to infer the mechanism from status codes alone, because nothing
    # kept what the refusal actually SAID — we knew only "403, text/html".
    # A vendor name or a reference id in the body is the difference between
    # "some WAF" and a specific rule someone can ask ICE about.
    #
    # Deliberately narrow: a fixed allowlist of response headers, and body text
    # with tags stripped and capped. Request headers are never recorded (ours
    # would be echoed back), Set-Cookie is never recorded, and the cap keeps a
    # hostile body from becoming a log-injection surface.
    "block_signature": None,
}

# Response headers safe to keep from a refusal. Everything else — Set-Cookie
# above all — is dropped rather than filtered, so a header added by a future
# WAF cannot leak by default.
_SIGNATURE_HEADERS = ("Server", "Content-Type", "X-Reference-Error", "X-Akamai-Request-ID")
_SIGNATURE_BODY_CHARS = 200


def _record_block_signature(r) -> None:
    """Keep one sanitised fingerprint of the first refusal in a run."""
    if _RUN_STATS["block_signature"] is not None:
        return
    try:
        body = re.sub(r"<[^>]+>", " ", r.text or "")
        body = re.sub(r"\s+", " ", body).strip()[:_SIGNATURE_BODY_CHARS]
    except Exception:  # noqa: BLE001 — a signature must never break a run
        body = ""
    _RUN_STATS["block_signature"] = {
        "status": r.status_code,
        "url_path": urlsplit(r.url).path if getattr(r, "url", None) else None,
        "headers": {k: r.headers.get(k) for k in _SIGNATURE_HEADERS if r.headers.get(k)},
        "body_excerpt": body,
    }

# The section being fetched right now. A 403 storm skips the rest of it, and
# this is what names the hole in the log, the Telegram line, the research page
# and the data-map run record.
_CURRENT_SECTION: dict[str, str] = {"label": "startup"}


def _load_cursor() -> dict:
    try:
        return json.loads(STOCK_REPORT_CURSOR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cursor(d: date, last_tried: str | None, *, keep_tried: bool = True) -> None:
    """Remember the last second ruled out for `d`. Cleared on success — a
    found day never needs resuming. `tried_tier1` survives either way: it is a
    different fact with a different lifetime (see _mark_tier1_tried)."""
    try:
        cur = _load_cursor() if keep_tried else {}
        payload: dict = {} if last_tried is None else {"date": d.isoformat(), "last_tried": last_tried}
        if keep_tried and cur.get("tried_tier1"):
            payload["tried_tier1"] = cur["tried_tier1"]
        STOCK_REPORT_CURSOR_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — telemetry must never break the run
        print(f"  ! cursor write failed: {e}")


# Dates whose tier-1 guess-list has already been walked and missed. Durable,
# because the answer does not change: tier-1 is a fixed set of seconds learned
# from OTHER days, so re-running it against the same date re-asks a question
# already answered no. 2026-08-31 is the case in point — swept to exhaustion on
# 1 Sep, still absent, and every run since has spent 50 more GETs on it.
TIER1_TRIED_KEEP = 60


def _tier1_already_tried(d: date) -> bool:
    return d.isoformat() in (_load_cursor().get("tried_tier1") or [])


def _mark_tier1_tried(d: date) -> None:
    try:
        cur = _load_cursor()
        tried = [x for x in (cur.get("tried_tier1") or []) if x != d.isoformat()]
        tried.append(d.isoformat())
        cur["tried_tier1"] = sorted(tried)[-TIER1_TRIED_KEEP:]
        STOCK_REPORT_CURSOR_PATH.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  ! tier1-tried write failed: {e}")


def _sweep_candidate_count() -> int:
    """Seconds in the sweep window.

    Both bounds are inclusive and now carry their own seconds, so this is a
    plain count. The old form gave only minutes and had to add the implied 59
    itself — which is how the miss alert once claimed a window 59 seconds
    smaller than the one actually walked.
    """
    lo, hi = STOCK_REPORT_SWEEP_RANGE
    return _hhmmss_to_s(hi) - _hhmmss_to_s(lo) + 1


def _notify_late_release(day: date) -> None:
    """Say so when the sweep covered its whole window and the report was not in
    it. The window is deliberately narrow, so this is a designed outcome, not a
    failure — but it is one that needs a human, because the only way to recover
    the session is for someone to supply the publish second. Silence would turn
    an announced trade-off back into a silent hole."""
    lo, hi = STOCK_REPORT_SWEEP_RANGE
    win = (f"{lo[0]:02d}:{lo[1]:02d}:{lo[2]:02d}–"
           f"{hi[0]:02d}:{hi[1]:02d}:{hi[2]:02d}")
    # Report the WINDOW first, with the GET count as a subordinate detail.
    # Printing sweep_gets alone read as though the sweep had stopped short \u2014
    # the first of these said "1870 seconds checked" against a 1,920-second
    # window, which looks like a run that gave up 50 short. It had not: tier 1
    # covers ~50 candidates before the sweep starts and the sweep does not
    # re-request them. That the window was fully covered is the load-bearing
    # fact: it is what makes "did not publish here" a conclusion rather than a
    # timeout, and it is exactly what the narrow window was chosen to buy.
    candidates = _sweep_candidate_count()
    text = (f"\u26a0\ufe0f ICE robusta stock report \u2014 missed, late release\n"
            f"{day.isoformat()} did not publish inside {win} UTC.\n"
            f"Swept the full window \u2014 {candidates:,} candidate seconds "
            f"({_RUN_STATS['sweep_gets']:,} requests after tier-1 overlap).\n"
            f"Add the publish time on the Admin research page to backfill it.")
    print(f"[robusta] LATE RELEASE — {day} not in {win}")
    _telegram(text, tag="robusta")


def _telegram(text: str, *, tag: str) -> None:
    """Send one line to the ops chat, or say in the log why it could not."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print(f"[{tag}] telegram not configured — not sending")
        return
    try:
        import requests as _rq
        _rq.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                 data={"chat_id": chat, "text": text}, timeout=20)
    except Exception as e:  # noqa: BLE001 — never fail the run on a notification
        print(f"[{tag}] telegram send failed: {e}")


# ── Edge-triggered block notification ────────────────────────────────────────
# Same model as data/alert_state.json (threshold_alerts.py): a condition fires
# ONCE when it turns true, then disarms, and re-arms only after it has been
# observed false again. Run 33978343636 sent the identical block message three
# times in fourteen minutes; a block that persists for a day would have sent it
# on every run. The message is worth reading the first time and is noise after.
#
# State is committed so it survives the runner, exactly like alert_state.json.
# parents[4] is the repo root — same anchor as OUT_DIR above. parents[3] is
# backend/, which quietly created backend/data/ instead.
# PORTED TO 620 — 619 anchors this on the repo-root data/ directory, which
# holds its private stores. Fetch state belongs with the fetcher here.
BLOCK_STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "ice_block_state.json"
BLOCK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _run_label() -> str:
    """Which job is speaking — the workflow's own name when GitHub supplies it.

    The message used to say "ICE certified stocks" whatever invoked it, so a
    block met by 1.14 (monthly reports) was reported under 1.13's name. That is
    why run 33978343636 was read as three runs of the wrong workflow.
    """
    return os.environ.get("GITHUB_WORKFLOW") or "ICE certified stocks"


def _block_key(*, only_monthly: bool, skip_monthly: bool) -> str:
    """State key for the feed set this run covers, independent of the env."""
    if only_monthly:
        return "ice_monthly"
    return "ice_daily" if skip_monthly else "ice_full"


def _load_block_state() -> dict:
    try:
        return json.loads(BLOCK_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent/corrupt state must not stop a run
        return {}


def _save_block_state(state: dict) -> None:
    try:
        BLOCK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BLOCK_STATE_PATH.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — a notification must not fail a run
        print(f"[ice] could not write block state: {e}")


def _block_signature(blocked: list[dict], wholly_refused: bool) -> str:
    """What makes two blocks "the same event".

    The set of refused sections, plus whether anything at all got through. A
    different section going dark, or a partial block becoming a total one, is a
    materially different failure and alerts again.
    """
    return "|".join(sorted(b["section"] for b in blocked)) + \
           ("|all" if wholly_refused else "|partial")


def _notify_blocked_sections(*, only_monthly: bool = False,
                             skip_monthly: bool = False) -> bool:
    """Announce a 403 block once, and announce the recovery.

    Skipping a refused section is the right call — one refused source must not
    throw away the nine that served — but it leaves a hole that nothing else
    says out loud. The workflow's own notifier is `if: failure()`; the data-map
    run record reads GitHub's conclusion; the research page reads run outcomes.
    All three look at a skipped section and see a green run, which is exactly
    the shape that has cost this project data three times over: reports success
    while collecting nothing.

    Returns True when this run met a block, so the caller can tell the workflow
    not to stack a second, contradictory "failed" line on top of this one.
    """
    blocked = _RUN_STATS["blocked_sections"]
    key = _block_key(only_monthly=only_monthly, skip_monthly=skip_monthly)
    state = _load_block_state()
    prev = state.get(key) or {"armed": True, "signature": None, "last_fired": None}
    now = datetime.now(UTC).isoformat(timespec="seconds")

    if not blocked:
        # Recovery, but only on evidence. A run that fetched nothing because it
        # already held everything has not shown that ICE is serving again, and
        # saying "RECOVERED" off the back of it would be a guess.
        if not prev.get("armed") and _RUN_STATS["ok_200"] > 0:
            _telegram(
                f"✅ ICE RECOVERED — {_run_label()}\n"
                f"{_RUN_STATS['ok_200']:,} of {_RUN_STATS['requests']:,} requests served. "
                f"New data captured.",
                tag="ice")
            print("[ice] RECOVERED — feed serving again")
            state[key] = {"armed": True, "signature": None,
                          "last_fired": prev.get("last_fired"), "recovered_at": now}
            _save_block_state(state)
        return False

    wholly_refused = _RUN_STATS["ok_200"] == 0 and _RUN_STATS["requests"] > 0
    total = _RUN_STATS["sections"] or len(blocked)
    sig = _block_signature(blocked, wholly_refused)

    print(f"[ice] 403 BLOCK — skipped {len(blocked)} of {total} sections: "
          f"{', '.join(b['section'] for b in blocked)}")

    # Armed, or a materially different failure than the one already reported.
    if prev.get("armed") or prev.get("signature") != sig:
        if wholly_refused:
            lines = [f"\U0001f6ab ICE BLOCKED — {_run_label()}",
                     "Every request refused. No new data was captured."]
        else:
            lines = [f"⚠️ ICE DEGRADED — {_run_label()}",
                     f"{len(blocked)} of {total} sections refused; the run kept the rest."]
        for b in blocked:
            lines.append(
                f"· {b['section']} — blocked after {b['after_403s']} x 403, "
                f"{b['skipped_requests']:,} further request(s) skipped")
        lines.append(
            f"Run totals: {_RUN_STATS['requests']:,} requests, {_RUN_STATS['ok_200']:,} x 200, "
            f"{_RUN_STATS['http_403']:,} x 403.")
        lines.append("Existing data is preserved and left explicitly stale. A 403 is a "
                     "per-IP block on the runner, not a pacing problem — the next "
                     "scheduled run draws a different runner. Further identical blocks "
                     "stay quiet until this one clears.")
        _telegram("\n".join(lines), tag="ice")
        state[key] = {"armed": False, "signature": sig, "last_fired": now}
        _save_block_state(state)
    else:
        print(f"[ice] identical block already reported at {prev.get('last_fired')} "
              f"— not sending again")
    return True

# The activity panel joins on the YAML basename, not the display title: a
# title is mutable and the Actions API caches it per run.
_WORKFLOW_NAME = "1.13 – ICE Certified Stocks (arabica + robusta)"
_WORKFLOW_FILE = "scraper-ice-certified-stocks.yml"


def _publish_degradation() -> None:
    """Put this run's skipped sections where the data-map run record can see it.

    That panel reads GitHub conclusions, and a 403 skip leaves the conclusion
    green — so without this row the run is indistinguishable from a clean one
    in the one place built to catch pipelines that are green while their data
    sits still.
    """
    blocked = _RUN_STATS["blocked_sections"]
    if not blocked:
        return
    total = _RUN_STATS["sections"] or len(blocked)
    skipped = sum(b["skipped_requests"] for b in blocked)
    run_degradations.record(
        workflow=_WORKFLOW_NAME,
        file=_WORKFLOW_FILE,
        kind="http_403",
        detail=(f"ICE refused {len(blocked)} of {total} fetch sections with 403 "
                f"(a per-IP block, not rate limiting). Those sections were skipped so the "
                f"rest of the run could finish; {skipped:,} further request(s) were given "
                f"up and their data is missing from this run."),
        items=[f"{b['section']} — after {b['after_403s']} x 403, "
               f"{b['skipped_requests']:,} request(s) skipped" for b in blocked],
    )


def _record_run_stats(outcome: str, sweep_day: date | None) -> None:
    try:
        prev = json.loads(RUN_STATS_PATH.read_text(encoding="utf-8")).get("runs", [])
    except Exception:
        prev = []
    waits = _RUN_STATS["retry_after_waits"]
    prev.append({
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sweep_day": sweep_day.isoformat() if sweep_day else None,
        "outcome": outcome,
        "http_429": _RUN_STATS["http_429"],
        "http_404": _RUN_STATS["http_404"],
        "retry_after_count": len(waits),
        "retry_after_total_s": round(sum(waits), 1),
        "retry_after_max_s": max(waits) if waits else 0,
        "throttle_bumps": _RUN_STATS["throttle_bumps"],
        "aborted_by_429": bool(_RUN_STATS["aborted_by_429"]),
        # The 403 side of the record. It was missing entirely: `outcome` could
        # read "aborted_403" while not one field in the row said how many 403s
        # there were, how many requests succeeded, or which section was cut
        # short — so the research page could only ever report rate limiting,
        # and a block looked like a quiet day.
        "http_403": _RUN_STATS["http_403"],
        "ok_200": _RUN_STATS["ok_200"],
        "aborted_by_403": bool(_RUN_STATS["aborted_by_403"]),
        "sections": _RUN_STATS["sections"],
        "blocked_sections": list(_RUN_STATS["blocked_sections"]),
        # Sanitised fingerprint of the first refusal — see
        # _record_block_signature. Absent on a run that met no 403.
        "block_signature": _RUN_STATS["block_signature"],
        "sweep_gets": _RUN_STATS["sweep_gets"],
        "resumed_from": _RUN_STATS["resumed_from"],
        "requests": _RUN_STATS["requests"],
        "wait_publicdocs_s": round(_RUN_STATS["wait_publicdocs_s"], 1),
        "wait_marketdata_s": round(_RUN_STATS["wait_marketdata_s"], 1),
    })
    try:
        RUN_STATS_PATH.write_text(
            json.dumps({"runs": prev[-RUN_STATS_KEEP:]}, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  ! run-stats write failed: {e}")


def _load_stock_report_hits() -> list[dict]:
    if not STOCK_REPORT_HITS_PATH.exists():
        return []
    try:
        return json.loads(STOCK_REPORT_HITS_PATH.read_text(encoding="utf-8")).get("hits", [])
    except Exception:
        return []

def _record_stock_report_hit(d: date, hhmmss: str) -> None:
    # One entry per date (latest wins), capped — the file is committed back by
    # the workflow so tier-1 learns across runs, so keep it small and clean.
    hits = [h for h in _load_stock_report_hits() if h.get("date") != d.isoformat()]
    hits.append({"date": d.isoformat(), "hhmmss": hhmmss})
    hits = hits[-400:]
    STOCK_REPORT_HITS_PATH.write_text(
        json.dumps({"hits": hits}, indent=2), encoding="utf-8",
    )


def _recorded_time_for(d: date) -> str | None:
    """The publish second already recorded for this exact date, if any."""
    iso = d.isoformat()
    for h in _load_stock_report_hits():
        if h.get("date") == iso and h.get("hhmmss"):
            return h["hhmmss"]
    return None


def _hhmmss_pm(t: str, pm: int) -> list[str]:
    """`t` plus ±1..pm seconds (nearest-first) as HHMMSS strings — absorbs the
    day-to-day second-drift in ICE's publish time so a tier-1 near-miss still
    hits without falling through to the full sweep."""
    try:
        base = int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])
    except (ValueError, IndexError):
        return [t]
    out = [t]
    for k in range(1, pm + 1):
        for s in (base - k, base + k):
            if 0 <= s < 86400:
                hh, rem = divmod(s, 3600)
                mm, ss = divmod(rem, 60)
                out.append(f"{hh:02d}{mm:02d}{ss:02d}")
    return out


def _stock_report_tier1_times() -> tuple[str, ...]:
    """Top-K most-frequent HHMMSS from the hits log, each widened ±2s."""
    counts: dict[str, int] = defaultdict(int)
    for h in _load_stock_report_hits():
        if h.get("hhmmss"):
            counts[h["hhmmss"]] += 1
    most_common = [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:STOCK_REPORT_TIER1_K]]
    # Bootstrap guesses until the hits log accumulates real captures.
    base = most_common or ["103021", "103126", "103045"]
    out: list[str] = []
    for t in base:                       # exact (by frequency) first, then ±2s
        for e in _hhmmss_pm(t, 2):
            if e not in out:
                out.append(e)
    return tuple(out)

def _stock_report_sweep_times() -> list[str]:
    """Every HH:MM:SS in the configured publish window (inclusive).

    Walks SECONDS, not whole minutes. The minute-based version could only ever
    start and end on a :00/:59 boundary, so a window of 10:29:50 was not
    expressible — it silently became 10:29:00 and bought 50 requests of nothing.
    """
    lo, hi = STOCK_REPORT_SWEEP_RANGE
    out: list[str] = []
    for total in range(_hhmmss_to_s(lo), _hhmmss_to_s(hi) + 1):
        hh, rem = divmod(total, 3600)
        mm, ss = divmod(rem, 60)
        out.append(f"{hh:02d}{mm:02d}{ss:02d}")
    return out

# Magic-byte / content-type expectations per source — used to flag "200 OK but
# it's an HTML error page" responses that would otherwise be swallowed silently
# by the parsers.
_EXPECT_BY_NAME: dict[str, tuple] = {
    "arabica_xls":      ("application/vnd.ms-excel",                                       b"\xd0\xcf\x11\xe0"),
    "arabica_ageing":   ("application/vnd.ms-excel",                                       b"\xd0\xcf\x11\xe0"),
    "stock_report":     ("text/csv",                                                       b'"'),
    "age_allowance":    ("application/vnd.openxmlformats-officedocument.spreadsheetml",    b"PK\x03\x04"),
    "grading_overview": ("application/pdf",                                                b"%PDF"),
    "infested_warrant": ("application/pdf",                                                b"%PDF"),
    "gradings":         ("text/plain",                                                     b""),
    "grading_appeals":  ("text/plain",                                                     b""),
    "iss_recv_daily":   ("text/plain",                                                     b""),
    "iss_recv_monthly": ("text/plain",                                                     b""),
    "tenders":          ("text/plain",                                                     b""),
}


def _biz_days_back(start: date, n: int) -> list[date]:
    out: list[date] = []
    cur = start
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= timedelta(days=1)
    return out


def _pull_month_end(pull_fn, cal_month_end: date, *, amplitude: int = 2):
    """Resolve a robusta monthly report (age-allowance / iss-recv) whose file
    ICE dates by a business day near the calendar month-end. When the month-end
    falls on a weekend the file may be dated the last Friday (month-end −1/−2)
    OR the next Monday (+1/+2), so probe month-end ± `amplitude` days,
    nearest-first — e.g. 2026-05-31 (Sun) resolves to Fri 05-29 or Mon 06-01.
    Returns (resolved_date, url, parsed); parsed is None if none resolved."""
    offsets = [0]
    for k in range(1, amplitude + 1):
        offsets += [-k, k]              # 0, -1, +1, -2, +2 — nearest first
    for off in offsets:
        cand = cal_month_end + timedelta(days=off)
        url, parsed = pull_fn(cand)
        if parsed is not None:
            return cand, url, parsed
    return cal_month_end, None, None


def _throttle_for(url: str) -> float:
    return _THROTTLE["marketdata"] if "/marketdata/" in url else _THROTTLE["public"]


def _http_get(url: str, *, source: str | None = None, _retry: bool = False) -> requests.Response | None:
    # Checked BEFORE the try, because the `finally` below sleeps the throttle on
    # every exit path. With the check inside it, an aborted run kept paying the
    # full 5s/req for calls it never made: run 33854928072 logged 302 requests
    # of which only 8 were real, and spent 24.6 minutes asleep proving a block
    # it had already detected in the first 8. Aborting has to stop the clock,
    # not just the fetching.
    if _RATE_STATE["aborted"] or _RATE_STATE["section_blocked"]:
        if _RATE_STATE["section_blocked"] and _RUN_STATS["blocked_sections"]:
            _RUN_STATS["blocked_sections"][-1]["skipped_requests"] += 1
        return None
    throttle = _throttle_for(url)
    try:
        r = requests.get(url, headers=F.HEADERS, timeout=TIMEOUT, allow_redirects=True)

        # 429 → respect Retry-After (capped), back off, retry exactly once.
        if r.status_code == 429:
            if _retry:
                # Retry already done; give up on this URL and let the run continue.
                _RATE_STATE["consecutive_429s"] += 1
                _RUN_STATS["http_429"] += 1
                if _RATE_STATE["consecutive_429s"] >= TOO_MANY_429S:
                    _RATE_STATE["aborted"] = 1
                    _RUN_STATS["aborted_by_429"] = 1
                    print(f"  ! {TOO_MANY_429S} consecutive 429s — aborting remaining fetches")
                ctype = r.headers.get("Content-Type", "")[:40]
                print(f"  ! HTTP 429 (after retry) ({ctype}) {url}")
                return r
            # Read Retry-After, then apply our caps:
            #   • if Akamai asks > RETRY_AFTER_GIVE_UP_S (10 min) we abort — the
            #     IP is in penalty box, no point waiting hours per URL.
            #   • otherwise cap at RETRY_AFTER_MAX_S (90 s); long enough for the
            #     rolling window to drain, short enough not to burn the timeout.
            raw_after = 60
            try:
                raw_after = max(int(r.headers.get("Retry-After", "60")), 30)
            except ValueError:
                pass
            if raw_after > RETRY_AFTER_GIVE_UP_S:
                _RATE_STATE["aborted"] = 1
                _RUN_STATS["http_429"] += 1
                _RUN_STATS["aborted_by_429"] = 1
                print(f"  ! HTTP 429 with Retry-After={raw_after}s — too long, aborting "
                      f"remaining fetches: {url}")
                return r
            wait_s = min(raw_after, RETRY_AFTER_MAX_S)
            _RUN_STATS["http_429"] += 1
            _RUN_STATS["retry_after_waits"].append(wait_s)
            _RUN_STATS["throttle_bumps"] += 1
            # Self-tune: bump the matched path's throttle so subsequent calls
            # slow down too (capped). Applies to BOTH /publicdocs/ and
            # /marketdata/ — the 180-day run discovered both have rate limits.
            path_key = "marketdata" if "/marketdata/" in url else "public"
            _THROTTLE[path_key] = min(_THROTTLE[path_key] * 1.3, _THROTTLE_CAP)
            print(f"  ! HTTP 429 → sleeping {wait_s}s (Retry-After={raw_after}s); "
                  f"bumping {path_key} throttle to {_THROTTLE[path_key]:.1f}s/req: {url}")
            time.sleep(wait_s)
            return _http_get(url, source=source, _retry=True)

        if r.status_code == 200:
            _RATE_STATE["consecutive_429s"] = 0
            _RATE_STATE["consecutive_403s"] = 0
            _RUN_STATS["ok_200"] += 1
        else:
            ctype = r.headers.get("Content-Type", "")[:40]
            if r.status_code == 404:
                _RUN_STATS["http_404"] += 1
                # A 404 is the normal "not this second" answer during the sweep
                # and says nothing about whether we are blocked.
            elif r.status_code == 403:
                _RUN_STATS["http_403"] += 1
                _RATE_STATE["consecutive_403s"] += 1
                _record_block_signature(r)
                if _RATE_STATE["consecutive_403s"] >= TOO_MANY_403S:
                    if not _RATE_STATE["section_blocked"]:
                        print(f"  ! {TOO_MANY_403S} consecutive 403s — this source is being "
                              f"refused, not rate-limited. Skipping the rest of "
                              f"'{_CURRENT_SECTION['label']}' and moving on; a longer interval "
                              f"would not help and the sweep cannot match a refused response.")
                        _record_section_block()
                    _RATE_STATE["section_blocked"] = 1
            print(f"  ! HTTP {r.status_code} ({ctype}) {url}")
            return r

        # 200 OK but wrong shape (e.g. HTML error page) — treat as miss.
        if source and _wrong_shape(source, r):
            return None
        return r
    except requests.exceptions.RequestException as e:
        print(f"  ! {type(e).__name__}: {url} — {e}")
        return None
    finally:
        # Attribute the pause to the host that dictated it. `throttle` was read
        # at entry, so a self-bump mid-call is counted from the next request on.
        _RUN_STATS["requests"] += 1
        key = "wait_marketdata_s" if "/marketdata/" in url else "wait_publicdocs_s"
        _RUN_STATS[key] += throttle
        time.sleep(throttle)


def _safe_parse(parse_fn, source: str, day: date | None, raw, **kwargs):
    """Wrap a parser; log & swallow on failure so the run continues.
    `kwargs` are forwarded to the parser (e.g. source_url / month_end for
    parsers that need extra context beyond the raw bytes)."""
    try:
        if kwargs:
            return parse_fn(raw, **{**kwargs, "month_end": day} if day else kwargs)
        return parse_fn(raw)
    except Exception as e:  # noqa: BLE001
        print(f"  ! parse {source} {day}: {type(e).__name__}: {e}")
        return None


def _wrong_shape(source: str, r: requests.Response) -> bool:
    """Return True (and log) when a 200 response doesn't match the source's
    expected content-type / magic bytes — the classic 'Akamai serves 200 with
    HTML error body' case that otherwise passes silently into the parsers."""
    expected = _EXPECT_BY_NAME.get(source)
    if not expected:
        return False
    ct_prefix, magic = expected
    ctype = (r.headers.get("Content-Type", "") or "").lower()
    raw = r.content or b""
    bad = False
    if ct_prefix and not ctype.startswith(ct_prefix.lower()):
        if "text/html" in ctype:
            bad = True
    if magic and raw[:len(magic)] != magic:
        if raw[:5] in (b"<!DOC", b"<html", b"<HTML", b"<HtmL"):
            bad = True
    if bad:
        print(f"  ! WRONG-SHAPE {source}: ct={ctype[:40]!r} head={raw[:80]!r}")
    return bad


# ── Per-source pull functions ────────────────────────────────────────────────

def pull_arabica_xls(d: date) -> tuple[str, dict | None]:
    url = F.ARABICA_DAILY_XLS.format(yyyymmdd=F.yyyymmdd(d))
    r = _http_get(url, source="arabica_xls")
    if not r or r.status_code != 200 or not r.content:
        return url, None
    return url, _safe_parse(parse_arabica_xls, "arabica_xls", d, r.content)


def pull_arabica_ageing(month_end: date) -> tuple[str, dict | None]:
    """Monthly Arabica ageing report — last-business-day URL. Caller is the
    monthly cron, which seeds `month_end` to the previous month's last day.
    The parser returns the per-(origin, year-band) bag matrix the panel needs;
    failure modes (404, blocked, schema drift) all surface as `parsed=None`.
    """
    url = F.ARABICA_AGEING_XLS.format(yyyymmdd=F.yyyymmdd(month_end))
    r = _http_get(url, source="arabica_ageing")
    if not r or r.status_code != 200 or not r.content:
        return url, None
    return url, _safe_parse(parse_arabica_ageing, "arabica_ageing", month_end, r.content, source_url=url)


def pull_stock_report(d: date, *, sweep: bool = True) -> tuple[str | None, dict | None]:
    """Resolve the robusta stock CSV, whose filename carries the publish SECOND.

    Three tiers, each ruling out what the one before it settled. No second is
    requested twice for a given date.

      TIER 0   the second already recorded for THIS date, if any. One GET, and
               a hit ends the search. Retention is confirmed (probe 0.18), so a
               known day stays one GET forever — which is also what makes a
               missed day recoverable the moment a human supplies its second.

      TIER 1   the top-K most frequent seconds across the WHOLE hit log — every
               date, not this one — each widened ±2s, so ≤~50 candidates, minus
               anything tier 0 already settled. It is a guess drawn from other
               days' history and has no relationship to tier 0 beyond skipping
               it. It runs as a real fast path, ahead of the sweep.
                 · On a date it has already walked and missed, it is not re-run
                   — a fixed list re-asks a question already answered no.

      TIER 2   sequential sweep of STOCK_REPORT_SWEEP_RANGE, ascending, one
               request every _STOCK_SWEEP_INTERVAL_S, skipping tier 0's second
               and every tier-1 time. Skipped when sweep=False. Coverage of the
               window is unchanged: what tier 1 asked is not re-asked, and
               nothing else is dropped.

    Sequential is not a tuning choice. ICE's /marketdata/ host 429s ANY
    concurrency — two parallel GETs once drew Retry-After=3600 and wiped the
    run — so the per-request interval is the only knob, and the hit log
    accumulating over time is the only thing that makes the search cheaper.

    Returns (url, parsed), or (None, None) if the day was not found.
    """
    def _try(hhmmss: str) -> tuple[str | None, dict | None]:
        url = F.ROBUSTA_STOCK_REPORT_CSV.format(yyyymmdd=F.yyyymmdd(d), hhmmss=hhmmss)
        r = _http_get(url, source="stock_report")
        if r and r.status_code == 200 and r.text:
            _record_stock_report_hit(d, hhmmss)
            return url, _safe_parse(parse_stock_report, "stock_report", d, r.text)
        return None, None

    # Tier 0 — the time we already know for THIS date.
    #
    # Retention is confirmed (probe 0.18, 2026-08-26: three reports from June
    # and August still served in late August, 200/579b). So once a day's publish
    # second is recorded, fetching it again is a single GET forever — no
    # guessing, no sweep. That makes every recorded day cheap to re-fetch and,
    # more usefully, makes a MISSED day recoverable the moment its second is
    # learned from any source, including by hand.
    known = _recorded_time_for(d)
    if known:
        url, parsed = _try(known)
        if url:
            return url, parsed
        print(f"  ! recorded time {known} for {d} no longer resolves — re-searching")

    # Reaching here means tier 0 ran and MISSED, so its second is now a settled
    # no for this date and nothing below should ask it again. Tier 1 could
    # (when the recorded second is also a popular one) and the sweep always did
    # (whenever it falls inside the window) — measured at exactly one duplicate
    # GET per such run. Small, but it is a request spent re-answering a question
    # this same run already answered, which is the one kind of request that can
    # never pay off.
    ruled_out = {known} if known else set()

    # Tier 1 — the seconds learned from OTHER days, tried against this one, as a
    # real fast path ahead of the sweep.
    #
    # It used to be deferred: on a sweep day every tier-1 time inside the window
    # was left out, on the grounds that the sweep reaches it anyway (#839). That
    # is true about COVERAGE and wrong about LATENCY. The sweep walks ascending
    # from 10:29:50, so a report published at 10:39:12 is the ~1,400th request;
    # the same second sits in the top-10 list and would be found in under fifty.
    # Deferring made every sweep day pay the full walk to reach a second it
    # already suspected.
    #
    # Trying it first is not a trade, because tier 2 now skips whatever tier 1
    # asked: in the worst case the same requests happen in a different order, in
    # the common case ~1,350 of them never happen at all.
    #
    # Still suppressed on a date it has already walked and missed — a fixed list
    # re-asks a question already answered no. 2026-08-31 was swept to exhaustion
    # on 1 Sep and is still absent; re-running the list only re-confirms that.
    tier1 = [t for t in _stock_report_tier1_times() if t not in ruled_out]
    if not sweep and _tier1_already_tried(d):
        print(f"  → tier-1 already missed for {d} — not re-asking ({len(tier1)} GETs saved)")
        tier1 = []
    elif tier1:
        print(f"  → tier-1: {len(tier1)} candidate second(s) tried before the sweep")

    for hhmmss in tier1:
        url, parsed = _try(hhmmss)
        if url:
            return url, parsed

    if not sweep:
        # Record the miss so the next run does not repeat this list verbatim.
        _mark_tier1_tried(d)
        return None, None

    # Tier 2 — sequential sweep. Concurrency bans the IP at any level, so the
    # only lever is the per-request interval (_STOCK_SWEEP_INTERVAL_S), applied
    # by temporarily overriding the marketdata throttle for the sweep and
    # restoring it after. _http_get keeps its 429 guard, so an over-fast
    # interval aborts cleanly rather than hammering.
    # Everything already asked for this date: the tier-1 times tried above, plus
    # tier 0's second. The sweep is the only place left that could repeat them.
    tried = set(tier1) | ruled_out
    # Resume where a killed run left off. The walk is monotonic and the
    # knowledge is durable — a second already answered 404 will not start
    # answering 200 later — so re-walking it is pure repeated cost. Only
    # honoured for the SAME day; a new day starts from the top.
    cur = _load_cursor()
    resume_after = cur.get("last_tried") if cur.get("date") == d.isoformat() else None
    if resume_after:
        _RUN_STATS["resumed_from"] = resume_after
        print(f"  → resuming sweep after {resume_after} (previous run stopped there)")

    saved_throttle = _THROTTLE["marketdata"]
    _THROTTLE["marketdata"] = _STOCK_SWEEP_INTERVAL_S
    last = None
    try:
        for hhmmss in _stock_report_sweep_times():
            if hhmmss in tried:
                continue
            if resume_after and hhmmss <= resume_after:
                continue
            last = hhmmss
            _RUN_STATS["sweep_gets"] += 1
            url, parsed = _try(hhmmss)
            if url:
                _save_cursor(d, None)          # found — nothing left to resume
                return url, parsed
            if _RATE_STATE["aborted"] or _RATE_STATE["section_blocked"]:
                break                          # refused: keep the cursor, stop here
        else:
            # for-else: the loop ran to completion without breaking, so every
            # second in the window answered 404. The file is not in here.
            _RUN_STATS["sweep_exhausted"] = True
    finally:
        _THROTTLE["marketdata"] = saved_throttle
        # Persist progress whether we were banned, timed out mid-loop, or simply
        # exhausted the window. Written on the way out so even an abort keeps it.
        if last:
            _save_cursor(d, last)
    return None, None


def pull_gradings(d: date, *, max_seq: int = 3) -> list[tuple[str, dict]]:
    """gradrc_*.txt has a -N sequence suffix (multiple panels possible per day)."""
    results: list[tuple[str, dict]] = []
    for n in range(1, max_seq + 1):
        url = F.ROBUSTA_GRADINGS_TXT.format(yymmdd=F.yymmdd(d), n=n)
        r = _http_get(url, source="gradings")
        if not r or r.status_code != 200 or not r.text:
            break  # no -2 if -1 missing
        parsed = _safe_parse(parse_gradings, "gradings", d, r.text)
        if parsed:
            results.append((url, parsed))
    return results


def pull_grading_appeals(d: date, *, max_seq: int = 3) -> list[tuple[str, dict]]:
    """Same shape as gradings; very rare (only when an appeal is filed)."""
    results: list[tuple[str, dict]] = []
    for n in range(1, max_seq + 1):
        url = F.ROBUSTA_GRADING_APPEALS.format(yymmdd=F.yymmdd(d), n=n)
        r = _http_get(url, source="grading_appeals")
        if not r or r.status_code != 200 or not r.text:
            break
        parsed = _safe_parse(parse_gradings, "grading_appeals", d, r.text)
        if parsed:
            results.append((url, parsed))
    return results


def pull_iss_recv_daily(d: date) -> tuple[str, dict | None]:
    url = F.ROBUSTA_ISS_RECV_DAILY.format(yymmdd=F.yymmdd(d))
    r = _http_get(url, source="iss_recv_daily")
    if not r or r.status_code != 200 or not r.text:
        return url, None
    return url, _safe_parse(parse_iss_recv_daily, "iss_recv_daily", d, r.text)


def pull_tenders(d: date) -> tuple[str, dict | None]:
    url = F.ROBUSTA_TENDERS.format(yymmdd=F.yymmdd(d))
    r = _http_get(url, source="tenders")
    if not r or r.status_code != 200 or not r.text:
        return url, None
    return url, _safe_parse(parse_tenders, "tenders", d, r.text)


def pull_grading_overview(d: date) -> tuple[str, dict | None]:
    url = F.ROBUSTA_GRADING_OVERVIEW_PDF.format(yymmdd=F.yymmdd(d))
    r = _http_get(url, source="grading_overview")
    if not r or r.status_code != 200 or not r.content:
        return url, None
    return url, _safe_parse(parse_grading_overview_pdf, "grading_overview", d, r.content)


def pull_infested_warrant(d: date) -> tuple[str, dict | None]:
    """Rare — only ~13 publications per year; most days 404."""
    url = F.ROBUSTA_INFESTED_WARRANT.format(yymmdd=F.yymmdd(d))
    r = _http_get(url, source="infested_warrant")
    if not r or r.status_code != 200 or not r.content:
        return url, None
    return url, _safe_parse(parse_infested_warrant_pdf, "infested_warrant", d, r.content)


def pull_iss_recv_monthly(month_end: date) -> tuple[str, dict | None]:
    url = F.ROBUSTA_ISS_RECV_MONTHLY.format(yymmdd=F.yymmdd(month_end))
    r = _http_get(url, source="iss_recv_monthly")
    if not r or r.status_code != 200 or not r.text:
        return url, None
    return url, _safe_parse(parse_iss_recv_monthly, "iss_recv_monthly", month_end, r.text)


def pull_age_allowance(month_end: date) -> tuple[str, dict | None]:
    url = F.ROBUSTA_AGE_ALLOWANCE_XLSX.format(yyyymmdd=F.yyyymmdd(month_end))
    r = _http_get(url, source="age_allowance")
    if not r or r.status_code != 200 or not r.content:
        return url, None
    return url, _safe_parse(parse_age_allowance_xlsx, "age_allowance", month_end, r.content)


# ── Snapshot reductions (rich parsed dict → flat per-day row) ────────────────

def _arabica_snapshot(d: date, parsed: dict) -> dict:
    # Keep full per-section hierarchy on each snapshot so the period-view drill-
    # down (port → group → origin) can read history, not just latest_detail.
    sections: dict[str, dict] = {}
    for key in ("total_certified", "transition", "pending_grading", "rebagging"):
        s = parsed.get(key) or {}
        if s.get("grand_total") or s.get("by_origin"):
            sections[key] = {
                "grand_total": s.get("grand_total", 0),
                "by_port":     s.get("by_port", {}),
                "by_group":    s.get("by_group", {}),
                "by_origin":   s.get("by_origin", {}),
            }
    tc = sections.get("total_certified", {})
    gt = parsed.get("grading_today") or {}
    snap = {
        "date":                 d.isoformat(),
        "report_date":          parsed.get("report_date"),
        # Headline scalars (kept flat for cheap reads):
        "total_bags":           tc.get("grand_total", 0),
        "transition_bags":      sections.get("transition", {}).get("grand_total", 0),
        "pending_grading_bags": sections.get("pending_grading", {}).get("grand_total", 0),
        "rebagging_bags":       sections.get("rebagging", {}).get("grand_total", 0),
        "passed_today_bags":    gt.get("passed_today_bags", 0),
        "failed_today_bags":    gt.get("failed_today_bags", 0),
        # Convenience rollups (still flat for the headline charts):
        "by_port":              tc.get("by_port", {}),
        "by_group":             tc.get("by_group", {}),
        # Full hierarchy — port × group × origin per section, drives drill-down.
        "sections":             sections,
    }
    # Per-(origin, port) grading detail — only present on action-day matrix
    # reports (June 2026+). Lets the certified-stocks model attribute the day's
    # gradings to real (origin, port) cohorts instead of distributing a scalar.
    # Shape: {origin: {by_port: {code: bags}, group, total}}. Omitted on
    # legacy/no-action days to keep the snapshot stream lean.
    passed_detail = gt.get("passed_detail")
    failed_detail = gt.get("failed_detail")
    if passed_detail and passed_detail.get("by_origin"):
        snap["passed_by_origin"] = passed_detail["by_origin"]
    if failed_detail and failed_detail.get("by_origin"):
        snap["failed_by_origin"] = failed_detail["by_origin"]
    return snap


def _robusta_snapshot(d: date, stock: dict | None, gradings_today: list[dict],
                      iss_recv_today: dict | None, tenders_today: dict | None) -> dict:
    sr_total = (stock or {}).get("grand_total") or {}
    lots_graded_today = sum(g["summary"]["lots_graded_today"] for g in gradings_today) if gradings_today else 0
    iss_total = (iss_recv_today or {}).get("grand_total") or {}
    tenders_total = (tenders_today or {}).get("totals_today") or {}
    total_lots = sr_total.get("with_val_cert", 0)
    return {
        "date":                 d.isoformat(),
        "cut_off_date":         (stock or {}).get("cut_off_date"),
        "total_lots_certified": total_lots,
        # Headline cross-market unit so anything ranking Arabica vs Robusta side
        # by side (data-map, alerting) can read one canonical number. 1 ICE
        # Robusta lot = 10 tonnes = 10,000 kg ÷ 60 kg/bag = 166.6667 bags.
        # Lot-denominated detail (by_port_lots, lots_sold_today, …) stays raw
        # because that's the ICE-native semantic traders work in.
        "total_bags_60kg_equivalent": round(total_lots * 10_000 / 60),
        "non_tend_lots":        sr_total.get("non_tend", 0),
        "suspended_lots":       sr_total.get("suspended", 0),
        "lots_graded_today":    lots_graded_today,
        "lots_sold_today":      iss_total.get("sold", 0),
        "lots_bought_today":    iss_total.get("bought", 0),
        "tenders_today":        tenders_total.get("originals", 0),
        "by_port_lots":         {p["port_id"]: p["with_val_cert"] for p in (stock or {}).get("ports", [])},
    }


# ── Merge-into-existing ──────────────────────────────────────────────────────
# Each run produces a window of the recent N days. To support both a one-off
# big backfill (e.g. 180 days) and a cheap daily cron (e.g. 3 days) without
# clobbering history, merge the new window into whatever's already on disk.

def _load_existing_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _merge_arabica(new: dict, old: dict) -> dict:
    by_date = {s["date"]: s for s in (old.get("snapshots") or [])}
    for s in new.get("snapshots") or []:
        by_date[s["date"]] = s                          # new overrides
    new["snapshots"] = sorted(by_date.values(), key=lambda s: s["date"])
    if not new.get("latest_detail") and old.get("latest_detail"):
        new["latest_detail"] = old["latest_detail"]
    # Preserve the monthly-sourced age_detail across daily (skip_monthly) runs,
    # which rebuild latest_detail from the daily XLS (no age data of its own).
    new_ld = new.get("latest_detail")
    old_ld = old.get("latest_detail") or {}
    if isinstance(new_ld, dict) and not new_ld.get("age_detail") and old_ld.get("age_detail"):
        new_ld["age_detail"] = old_ld["age_detail"]
        new_ld["age_detail_date"] = old_ld.get("age_detail_date")
    # Ageing report — keep the older snapshot when the current run didn't
    # land one (e.g. mid-month, no new file published yet). When a fresher
    # month_end comes in it naturally wins because we overwrite.
    if not new.get("ageing_report") and old.get("ageing_report"):
        new["ageing_report"]     = old["ageing_report"]
        new["ageing_report_url"] = old.get("ageing_report_url")
    if new["snapshots"]:
        new["as_of"] = new["snapshots"][-1]["date"]
    return new


def _gradings_per_port_month_origin(gradings_list: list) -> dict:
    """Tenderable lots as {port: {cohort 'YYYY-MM': {origin: lots}}} from the
    accumulated daily gradings feed — the daily-run equivalent of the workbook's
    sheet-2 per-month split that feeds the cohort-DNA pipeline. Cohort = the
    calendar month a lot was graded in; only tenderable lots enter the pool."""
    out: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for g in gradings_list or []:
        cohort = (g.get("date") or "")[:7]
        if len(cohort) != 7:
            continue
        for e in g.get("entries") or []:
            if e.get("tenderable") is False:
                continue
            port = e.get("port")
            origin = (e.get("origin") or "").strip()
            lots = e.get("lots") or 0
            if not port or not origin or lots <= 0:
                continue
            out[port][cohort][origin] += lots
    return {p: {c: dict(o) for c, o in bc.items()} for p, bc in out.items()}


def recompute_robusta_cohort_outflow(data: dict) -> dict:
    """Recompute implied_outflow + current_by_origin from the data the robusta
    JSON already accumulates (merged age_allowance with buckets + gradings feed +
    port_origin_history). This keeps the cohort-DNA feed fresh on every run
    instead of being frozen at the last manual workbook import.

    Existing implied_outflow month_ends are preserved (published history stays
    stable); only newly-available months are filled in. current_by_origin is
    recomputed to the latest ageing report. No-ops when inputs are missing."""
    monthly = data.get("monthly") or {}
    age_reports = [r for r in (monthly.get("age_allowance") or [])
                   if r.get("month_end") and r.get("valid", {}).get("buckets")]
    if not age_reports:
        return data
    gpmo = _gradings_per_port_month_origin((data.get("recent_activity") or {}).get("gradings") or [])
    cohort_dna = build_cohort_dna(gpmo)
    port_alltime_dna = build_port_alltime_dna(data.get("port_origin_history") or {})

    computed = build_implied_outflow(age_reports, cohort_dna, port_alltime_dna, gpmo)
    existing = {e["month_end"]: e for e in (monthly.get("implied_outflow") or [])}
    added = 0
    for e in computed:
        if e["month_end"] not in existing:
            existing[e["month_end"]] = e
            added += 1
    monthly["implied_outflow"] = sorted(existing.values(), key=lambda e: e["month_end"])

    latest = max(age_reports, key=lambda r: r["month_end"])
    cbo = build_current_by_origin(latest, cohort_dna, port_alltime_dna, gpmo)
    if cbo:
        monthly["current_by_origin"] = cbo
    data["monthly"] = monthly
    print(f"  → cohort outflow recomputed: +{added} month(s), "
          f"latest implied_outflow = {monthly['implied_outflow'][-1]['month_end']}")
    return data


# ── "do we already hold this period?" ────────────────────────────────────────
# These answer one narrow question: is the SPECIFIC target period already in
# the published JSON? They are not a "the file is old, stop looking" switch.
#
# The monthly job exists because ICE publishes late and irregularly — the
# iss/recv file has shown up mid-month — so the run must keep asking for any
# period it does not hold, however far back. What it must stop doing is asking
# again for a period it parsed weeks ago. _target_month() rolls forward on the
# 1st of every month, so the arabica check below starts failing again the
# moment a new report is due, and the fetch resumes untouched.


def _target_month() -> str:
    """The period a monthly run is chasing: the previous calendar month."""
    first = date.today().replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def _have_ageing_for(month_key: str) -> bool:
    """True when the stored arabica ageing report is already that month.

    One slot, holding the most recent report — so "already captured" means the
    slot's own month_end matches. An older report in the slot is exactly the
    case we still fetch for.
    """
    doc = _load_existing_json(OUT_DIR / "certified_stocks_arabica.json") or {}
    return ((doc.get("ageing_report") or {}).get("month_end") or "")[:7] == month_key


def _have_monthly(feed: str, key_field: str, month_key: str) -> bool:
    """True when the robusta monthly `feed` already carries `month_key`.

    A list, so hold is per-period: holding 2026-07 says nothing about 2026-08,
    and the walk-back still requests every month that is missing.
    """
    doc = _load_existing_json(OUT_DIR / "certified_stocks_robusta.json") or {}
    rows = (doc.get("monthly") or {}).get(feed) or []
    return any((r.get(key_field) or "")[:7] == month_key for r in rows)


def _merge_robusta(new: dict, old: dict) -> dict:
    # daily_fetched: union. This is the ledger of days whose per-day sources
    # (gradings, appeals, iss/recv, tenders, overview, infested) have actually
    # been requested. It has to be recorded rather than inferred, because a day
    # with no gradings and a day never fetched look identical in the data — and
    # guessing wrong either re-fetches forever or leaves a permanent hole.
    fetched = set(old.get("daily_fetched") or []) | set(new.get("daily_fetched") or [])
    new["daily_fetched"] = sorted(fetched)[-400:]

    # snapshots: union by date.
    by_date = {s["date"]: s for s in (old.get("snapshots") or [])}
    for s in new.get("snapshots") or []:
        by_date[s["date"]] = s
    new["snapshots"] = sorted(by_date.values(), key=lambda s: s["date"])

    # recent_activity: union by `date` field (the event keying).
    for key in ("gradings", "grading_appeals", "iss_recv_daily",
                "tenders", "grading_overview", "infested_warrants"):
        merged: dict[str, dict] = {}
        for e in (old.get("recent_activity") or {}).get(key, []):
            merged[e.get("date") or ""] = e
        for e in (new.get("recent_activity") or {}).get(key, []):
            merged[e.get("date") or ""] = e
        merged.pop("", None)
        new.setdefault("recent_activity", {})[key] = sorted(
            merged.values(), key=lambda e: e.get("date") or ""
        )

    # monthly: union by month key.
    def _merge_monthly(key: str, k_field: str) -> list:
        merged: dict[str, dict] = {}
        for e in (old.get("monthly") or {}).get(key, []):
            merged[e.get(k_field) or ""] = e
        for e in (new.get("monthly") or {}).get(key, []):
            merged[e.get(k_field) or ""] = e
        merged.pop("", None)
        return sorted(merged.values(), key=lambda e: e.get(k_field) or "")

    new.setdefault("monthly", {})["iss_recv_monthly"] = _merge_monthly("iss_recv_monthly", "month")
    new["monthly"]["age_allowance"] = _merge_monthly("age_allowance", "month_end")
    # Carry over the existing cohort-DNA outputs; recompute_robusta_cohort_outflow
    # (below) then extends implied_outflow with any newly-available months so the
    # feed no longer freezes at the last manual workbook import.
    if not new["monthly"].get("implied_outflow") and (old.get("monthly") or {}).get("implied_outflow"):
        new["monthly"]["implied_outflow"] = old["monthly"]["implied_outflow"]
    if not new["monthly"].get("current_by_origin") and (old.get("monthly") or {}).get("current_by_origin"):
        new["monthly"]["current_by_origin"] = old["monthly"]["current_by_origin"]

    # latest_detail: only overwrite if the new run actually captured a
    # stock_report — otherwise keep the older one so the panel keeps showing
    # the most recent good snapshot even when today's run missed.
    if not new.get("latest_detail", {}).get("stock_report") and old.get("latest_detail", {}).get("stock_report"):
        new["latest_detail"] = old["latest_detail"]

    # port_origin_history (workbook full-history lookup) — only the workbook
    # importer emits it. Preserve the older copy when the daily scraper run
    # doesn't carry one, so it survives across nightly merges.
    if not new.get("port_origin_history") and old.get("port_origin_history"):
        new["port_origin_history"] = old["port_origin_history"]

    # Now that age_allowance, gradings and port_origin_history are all merged,
    # recompute the cohort-DNA outflow so "cohort out" stays fresh each run.
    recompute_robusta_cohort_outflow(new)

    if new["snapshots"]:
        new["as_of"] = new["snapshots"][-1]["date"]
    return new

class AllRequestsRefused(RuntimeError):
    """Every HTTP request in the run failed. Raised so the job exits non-zero.

    On 2026-09-04 ICE began answering /marketdata/publicdocs/ with 403 and an
    HTML challenge body. The run made 1,368 requests, every one refused, then
    merged zero new data over the existing snapshots, wrote both JSONs, and
    reported success — the only visible symptom was data quietly going stale.
    Per-source failures are deliberately non-fatal (one missing report must not
    lose the other nine), but "not one request succeeded" is not a per-source
    failure, it is the feed being gone, and it has to be loud.
    """


def _assert_not_wholly_refused() -> None:
    """Fail the run when every request was refused. A run that fetched nothing
    because nothing was published is fine and stays silent — that run makes few
    or no requests. This only fires when we ASKED and were refused every time.
    """
    reqs, ok = _RUN_STATS["requests"], _RUN_STATS["ok_200"]
    if reqs >= _REFUSED_MIN_REQUESTS and ok == 0:
        raise AllRequestsRefused(
            f"{reqs} requests, 0 succeeded "
            f"({_RUN_STATS['http_403']} x 403, {_RUN_STATS['http_429']} x 429, "
            f"{_RUN_STATS['http_404']} x 404). The feed is refusing us, not idle "
            f"— data was NOT refreshed. A 403 with an HTML body is a block, not "
            f"a rate limit: changing the request interval will not clear it."
        )


# Below this, a run genuinely had nothing to ask for (a quiet day, or every
# source served from cursor) and zero successes means nothing.
_REFUSED_MIN_REQUESTS = 10


def run(days_back: int = 30, write: bool = True, merge: bool = True,
        skip_monthly: bool = False, only_monthly: bool = False) -> dict:
    # skip_monthly  → daily scraper: pull the daily feeds, skip the monthly
    #                 ageing / age-allowance reports (those have their own job).
    # only_monthly  → monthly scraper: pull ONLY the ageing / age-allowance
    #                 reports, skip the (heavy) daily feeds + robusta sweep.
    # The merge step preserves whichever half this run didn't fetch.
    # Anchor the window on the PREVIOUS business day. ICE publishes every
    # certified-stock report (arabica xls, robusta stock CSV, gradings, iss/
    # recv, tenders, overview, …) for the prior business day, so the current
    # day's file generally isn't posted yet when the daily cron runs. Targeting
    # yesterday gets the latest actually-published data for every feed and
    # avoids a wasted (and, for the robusta stock sweep, ~10-min) same-day fetch.
    # Time-aware anchor. ICE publishes a business day's full report set by
    # ~17:00 UTC (gradings ~17:02 London are the last). An early-UTC-morning
    # run must target the PRIOR business day; but when the run fires late in
    # the day (GH cron has been observed firing up to ~19h late — a Monday
    # 21:42 run once re-fetched Friday while Monday's files sat published),
    # TODAY's files are already out, so target today instead of lagging a day.
    _now = datetime.now(UTC)
    anchor = _now.date() if _now.hour >= 18 else _now.date() - timedelta(days=1)
    while anchor.weekday() >= 5:        # back over Sat/Sun to the last weekday
        anchor -= timedelta(days=1)
    days = _biz_days_back(anchor, days_back)
    days_sorted_asc = sorted(days)
    print(f"=== ICE certified-stocks pull · window = {days_back} biz days "
          f"({days_sorted_asc[0]} → {days_sorted_asc[-1]}; anchored on prior biz day) "
          f"[skip_monthly={skip_monthly} only_monthly={only_monthly}] ===\n")

    # ── Arabica: 1 source, loop dates ──
    # Ledger of days whose per-day robusta sources were requested. Empty on a
    # --only-monthly run, which skips that block; the merge unions it with the
    # stored list, so an empty one inherits rather than blanks.
    daily_fetched: list[str] = []
    arabica_snapshots: list[dict] = []
    arabica_latest: dict | None = None
    arabica_latest_date: date | None = None
    arabica_source_url: str | None = None
    arabica_errors: list[str] = []

    if not only_monthly:
        _begin_section("arabica daily xls")
        print(f"[arabica] daily xls, {len(days_sorted_asc)} days...")
        for d in days_sorted_asc:
            url, parsed = pull_arabica_xls(d)
            if parsed is None:
                arabica_errors.append(f"{d.isoformat()}: no file")
                continue
            arabica_snapshots.append(_arabica_snapshot(d, parsed))
            arabica_latest = parsed
            arabica_latest_date = d
            arabica_source_url = url
        print(f"  → {len(arabica_snapshots)} snapshots; {len(arabica_errors)} misses\n")

    # ── Arabica monthly ageing report ──
    # The file is dated around month-end but the exact day drifts (weekend /
    # holiday → published a few days early, occasionally late), so for each
    # recent month we try a small set of candidate dates (see
    # F.month_end_publish_candidates) and take the first that parses with data.
    # Walk back up to 2 months in case the latest isn't published yet.
    arabica_ageing: dict | None = None
    arabica_ageing_url: str | None = None
    if not skip_monthly and _have_ageing_for(_target_month()):
        # Already holding the TARGET period — not "we have some old report, so
        # never look again". The distinction is the whole point of this job:
        # next month _target_month() moves on, this check fails, and the fetch
        # runs exactly as before. Skipping here only ever removes a request for
        # a period already sitting in the JSON.
        print(f"[arabica] ageing report {_target_month()} already captured — not re-fetching")
    elif not skip_monthly:
        # ONE section for the whole walk-back, not one per month.
        #
        # This used to sit inside the loop below, which reset consecutive_403s
        # on every iteration — and since a month is only 6 candidates against a
        # TOO_MANY_403S of 8, the breaker could never trip here. Run
        # 33978343636 spent 18 refusals proving a block it should have called
        # after 8, three times over. It also counted 3 sections where there is
        # one, which is why that run's Telegram said "1 of 4" for a job with
        # two fetch sections.
        _begin_section("arabica ageing report")
        for back in (0, 1, 2):
            # Cursor from the REAL calendar date, not the business-day anchor:
            # when the 1st-3rd fall on Sat-Mon (Aug 2026), the anchor stays in
            # the PREVIOUS month and the newest month-end was never attempted.
            anchor = date.today().replace(day=1) - timedelta(days=1)   # last day of prev month
            for _ in range(back):
                anchor = anchor.replace(day=1) - timedelta(days=1)   # one more month back
            candidates = F.month_end_publish_candidates(anchor.year, anchor.month)
            print(f"[arabica] ageing report for {anchor.year}-{anchor.month:02d} "
                  f"(trying {len(candidates)} candidate dates)...")
            for cand in candidates:
                url, parsed = pull_arabica_ageing(cand)
                if parsed and parsed.get("grand_total", 0) > 0:
                    arabica_ageing = parsed
                    arabica_ageing_url = url
                    n_dim = (f"{len(parsed.get('origins', []))} origins" if parsed.get("origins")
                             else f"{len(parsed.get('ports', []))} ports")
                    print(f"  → captured {cand.isoformat()} ({parsed.get('grand_total', 0):,} bags across {n_dim})")
                    break
            if arabica_ageing is not None:
                break
            print(f"  → miss, will try {back+1} month(s) back")
    # (age_detail is folded into latest_detail AFTER the merge below, so a
    # monthly-only run patches it onto the carried-forward daily latest_detail.)


    # ── Robusta daily feeds (stock sweep + gradings/iss/tenders/overview) ──
    robusta_stocks: dict[date, dict] = {}
    robusta_stock_url: str | None = None
    gradings_all: list[dict] = []      # list of (date, url, parsed)
    appeals_all: list[dict] = []
    iss_recv_all: dict[date, dict] = {}
    tenders_all: dict[date, dict] = {}
    overview_all: dict[date, dict] = {}
    infested_all: list[dict] = []
    sweep_day: date | None = None      # also read by the telemetry after run()
    if not only_monthly:
        _begin_section("robusta stock report")
        # Header said "latest day only" for months while the loop below walked
        # FIVE. The reader who then sees three dates in the log has to go and
        # find out which is lying — so it now states what it does.
        print(f"[robusta] stock report (.csv, {len(days_sorted_asc[-5:])} recent days; "
              f"tier-2 sweep on the last only)...")
        #
        # A previous edit here cut this to the latest day alone, on the reading
        # that "ICE only keeps one per day" and the four older probes were 16
        # minutes of guaranteed 404s. That inference was wrong. The evidence was
        # a log line — "1 stock-report snapshots captured" out of five days —
        # which is equally explained by tier-1 GUESSING WRONG on the other four,
        # since the publish second is near-unique and tier-1 covers ~34% of days.
        # And the operator has since produced working URLs for 2026-06-10 and
        # 2026-06-29 months after the fact, which points the other way entirely.
        #
        # probe_stock_report_access.py settles it. Until it has run, the older
        # days are probed as they always were: if retention is real, those
        # probes are the only route back to a missed session, and a missed
        # session is a permanent hole in the record (verified: all three misses
        # in the June–August window have no snapshot from any source).
        # Recover holes FIRST — before the sweep, deliberately.
        #
        # Each is a single GET with a known-good URL: certain value, negligible
        # cost. The sweep below is the opposite — up to two hours, and it may
        # find nothing. Ordering the certain work behind the gamble means a run
        # that times out mid-sweep never reaches the recovery at all, which is
        # exactly what happened on run 32950531834: three holes that were three
        # requests away sat behind a full walk for an unrelated day.
        #
        # The candidate list comes from the hit log rather than the run window,
        # because holes are usually months old by the time their time is learned
        # (all three in the June–August window were), and a window wide enough
        # to reach them would drag every other per-day fetch along with it.
        stored = set()
        _existing = _load_existing_json(OUT_DIR / "certified_stocks_robusta.json")
        if _existing:
            stored = {s.get("date") for s in (_existing.get("snapshots") or [])}
        holes = [h["date"] for h in _load_stock_report_hits()
                 if h.get("date") not in ("bootstrap", None)
                 and h["date"] not in stored
                 and h["date"] not in {d.isoformat() for d in robusta_stocks}]
        if holes:
            print(f"[robusta] recovering {len(holes)} known-time hole(s): "
                  f"{', '.join(holes[:RECOVER_MAX])}")
            for iso in holes[:RECOVER_MAX]:
                dd = date.fromisoformat(iso)
                url, parsed = pull_stock_report(dd, sweep=False)
                if parsed is not None:
                    robusta_stocks[dd] = parsed
                    print(f"  ✓ recovered {iso}")
                else:
                    print(f"  ✗ {iso} still unresolved")

        recent_days = days_sorted_asc[-5:]
        sweep_day = recent_days[-1] if recent_days else None
        for d in recent_days:
            url, parsed = pull_stock_report(d, sweep=(d == sweep_day))
            if parsed is not None:
                robusta_stocks[d] = parsed
                robusta_stock_url = url
        if sweep_day is not None and sweep_day not in robusta_stocks \
                and _RUN_STATS["sweep_exhausted"]:
            _notify_late_release(sweep_day)
        print(f"  → {len(robusta_stocks)} stock-report snapshots captured "
              f"(tier-2 sweep day: {sweep_day})\n")


        _begin_section("robusta per-day sources")
        # ONE day per run — the anchor (prior business day), which is the only
        # day that can carry data we do not already have. The loop used to walk
        # all 7 window days every run: 6 sources x 7 days = 42 requests at the
        # 5s marketdata throttle, of which 14 day/source pairs were already
        # stored on 2026-09-04 and re-requested anyway. Volume against
        # /marketdata/publicdocs/ is what gets a runner IP blocked, so a request
        # that cannot teach us anything is not free.
        #
        # The 7-day window (#609) existed because a day falling between runs was
        # "lost for good". That is still a real risk and is still covered — but
        # by fetching the days actually MISSING from the ledger, not by
        # re-fetching six days that are already in hand. On a healthy day the
        # missing list is empty and this is exactly one day, as intended.
        _prev_fetched = set((_existing or {}).get("daily_fetched") or [])
        anchor_day = days_sorted_asc[-1]
        gap_days = [d for d in days_sorted_asc[:-1] if d.isoformat() not in _prev_fetched]
        daily_days = gap_days + [anchor_day]
        if gap_days:
            print(f"[robusta] per-day sources: {anchor_day} + {len(gap_days)} never-fetched "
                  f"gap day(s) ({', '.join(d.isoformat() for d in gap_days)})...")
        else:
            print(f"[robusta] per-day sources: {anchor_day} only (no gaps in the window)...")
        for d in daily_days:
            for url, parsed in pull_gradings(d):
                gradings_all.append({"date": d.isoformat(), "url": url, **parsed})
            for url, parsed in pull_grading_appeals(d):
                appeals_all.append({"date": d.isoformat(), "url": url, **parsed})
            _, parsed = pull_iss_recv_daily(d)
            if parsed: iss_recv_all[d] = parsed
            _, parsed = pull_tenders(d)
            if parsed: tenders_all[d] = parsed
            _, parsed = pull_grading_overview(d)
            if parsed: overview_all[d] = parsed
            _, parsed = pull_infested_warrant(d)
            if parsed: infested_all.append({"date": d.isoformat(), **parsed})
        daily_fetched = sorted({d.isoformat() for d in daily_days} | _prev_fetched)
        print(f"  → gradings={len(gradings_all)}  appeals={len(appeals_all)}  "
              f"iss/recv={len(iss_recv_all)}  tenders={len(tenders_all)}  "
              f"overview={len(overview_all)}  infested={len(infested_all)}  "
              f"({len(daily_days)} day(s) requested, {len(daily_days) * 6} GETs)\n")

    # ── Robusta monthly reports (iss/recv + age allowance) ──
    monthly_iss_recv: list[dict] = []
    age_allowance_list: list[dict] = []
    if not skip_monthly:
        _begin_section("robusta monthly reports")
        print("[robusta] monthly: iss/recv + age allowance (last 3 month-ends)...")
        # Walk back from current month's end through last 3 month-ends.
        # Real calendar date, not the business-day anchor (see arabica note).
        #
        # Each month-end is skipped only when THAT month is already in the
        # JSON. The walk-back still runs, so a month we do not hold is still
        # requested however old it is, and the newest month-end is requested on
        # every run until it lands — which is what the 5th/8th/11th crons are
        # for, since the iss/recv file has appeared as late as mid-month. What
        # goes away is re-downloading a period we already parsed. Measured
        # against the 2026-09-05 state of the JSONs, --only-monthly issues:
        #
        #   ICE serving (404s, nothing new published)   48 -> 10 requests
        #   ICE refusing (all 403)                      26 ->  8 requests
        #
        # The 10 that remain are the iss/recv candidates for 2026-08 and
        # 2026-06 — the two months genuinely absent from the file. Every
        # age_allowance month and the whole arabica section are held, so they
        # are not asked for at all.
        cursor = date.today().replace(day=1) - timedelta(days=1)   # calendar last day of prev month
        skipped_months: list[str] = []
        for _ in range(3):
            key = cursor.strftime("%Y-%m")
            want_iss = not _have_monthly("iss_recv_monthly", "month", key)
            want_age = not _have_monthly("age_allowance", "month_end", key)
            if not want_iss and not want_age:
                skipped_months.append(key)
            if want_iss:
                _, _, parsed = _pull_month_end(pull_iss_recv_monthly, cursor)
                if parsed:
                    monthly_iss_recv.append(parsed)
            if want_age:
                me, _, parsed = _pull_month_end(pull_age_allowance, cursor)
                if parsed:
                    # Store the resolved (last-business-day) date so a weekend
                    # month-end like May 31 → May 29 keeps the report's real file date.
                    age_allowance_list.append({"month_end": me.isoformat(), **parsed})
            cursor = (cursor.replace(day=1) - timedelta(days=1))
        if skipped_months:
            print(f"  → already captured, not re-fetched: {', '.join(skipped_months)}")
        print(f"  → monthly_iss_recv={len(monthly_iss_recv)}  age_allowance={len(age_allowance_list)}\n")

    # ── Build robusta snapshots (one per business day with any data) ──
    robusta_snapshots: list[dict] = []
    # The run window PLUS anything the hole recovery pulled in. Recovered days
    # are months older than the window by definition, so iterating the window
    # alone fetched them and then silently dropped them before the merge —
    # which is exactly what happened on run 32958. The two August holes landed
    # (inside the 7-day window) and the three June ones did not.
    for d in sorted(set(days_sorted_asc) | set(robusta_stocks)):
        gradings_today = [g for g in gradings_all if g["date"] == d.isoformat()]
        snap = _robusta_snapshot(d, robusta_stocks.get(d), gradings_today,
                                  iss_recv_all.get(d), tenders_all.get(d))
        # Only keep snapshots that carry an actual stock report. A day with
        # grading/flow activity but a MISSING stock report would otherwise store
        # zero stock (by_port_lots={}), and the day-over-day stock diff would
        # read the whole certified stock as "decertified" (this produced the
        # phantom 2026-06-17 decertification of ~4,000 lots). Such days' grading
        # and flow activity still live in recent_activity, so nothing is lost.
        if snap.get("by_port_lots"):
            robusta_snapshots.append(snap)
    # Latest is the most recent day with stock_report data.
    robusta_latest_date = max(robusta_stocks.keys(), default=None)
    robusta_latest_stock = robusta_stocks.get(robusta_latest_date) if robusta_latest_date else None

    # ── Assemble JSONs ──
    now = datetime.now(UTC).isoformat(timespec="seconds")

    arabica_json = {
        "generated_at": now,
        "as_of":        arabica_latest_date.isoformat() if arabica_latest_date else None,
        "source_url":   arabica_source_url,
        "snapshots":    arabica_snapshots,
        "latest_detail": arabica_latest,
        # ICE C-contract monthly ageing report: per-(origin, year-band) bags.
        # Driven by the new pull_arabica_ageing() above; absent on days where
        # the file hasn't published yet — _merge_arabica keeps the older copy.
        "ageing_report": arabica_ageing,
        "ageing_report_url": arabica_ageing_url,
        "errors":       arabica_errors,
    }

    robusta_json = {
        "generated_at":  now,
        "as_of":         robusta_latest_date.isoformat() if robusta_latest_date else None,
        # Days whose per-day sources have actually been requested. Read back on
        # the next run so a day already fetched is not fetched again, and a day
        # genuinely missed still is.
        "daily_fetched": daily_fetched,
        "snapshots":     robusta_snapshots,
        "latest_detail": {
            "stock_report":     robusta_latest_stock,
            "stock_report_url": robusta_stock_url,
        },
        "recent_activity": {
            "gradings":          gradings_all,
            "grading_appeals":   appeals_all,
            "iss_recv_daily":    [{"date": d.isoformat(), **v} for d, v in sorted(iss_recv_all.items())],
            "tenders":           [{"date": d.isoformat(), **v} for d, v in sorted(tenders_all.items())],
            "grading_overview":  [{"date": d.isoformat(), **v} for d, v in sorted(overview_all.items())],
            "infested_warrants": infested_all,
        },
        "monthly": {
            "iss_recv_monthly": monthly_iss_recv,
            "age_allowance":    age_allowance_list,
        },
    }

    if merge:
        existing_a = _load_existing_json(OUT_DIR / "certified_stocks_arabica.json")
        existing_r = _load_existing_json(OUT_DIR / "certified_stocks_robusta.json")
        if existing_a:
            n_old = len(existing_a.get("snapshots") or [])
            arabica_json = _merge_arabica(arabica_json, existing_a)
            print(f"[merge] arabica: {n_old} existing snapshots → {len(arabica_json['snapshots'])} after merge")
        if existing_r:
            n_old = len(existing_r.get("snapshots") or [])
            robusta_json = _merge_robusta(robusta_json, existing_r)
            ra = robusta_json["recent_activity"]
            print(f"[merge] robusta: {n_old} existing snapshots → {len(robusta_json['snapshots'])} after merge "
                  f"(events: gradings={len(ra['gradings'])} iss={len(ra['iss_recv_daily'])} "
                  f"tend={len(ra['tenders'])} overview={len(ra['grading_overview'])} "
                  f"infested={len(ra['infested_warrants'])} appeals={len(ra['grading_appeals'])})")

    # The daily Arabica XLS carries no age data — fold the ageing report's
    # per-port day-buckets into latest_detail so the frontend's age fade / age
    # tiles render. Done AFTER the merge so a monthly-only run patches it onto
    # the carried-forward daily latest_detail. (A daily/skip-monthly run leaves
    # arabica_ageing None here and _merge_arabica preserves the prior age_detail.)
    if arabica_ageing and arabica_ageing.get("age_detail") and arabica_json.get("latest_detail"):
        arabica_json["latest_detail"]["age_detail"] = arabica_ageing["age_detail"]
        arabica_json["latest_detail"]["age_detail_date"] = arabica_ageing.get("month_end")

    # Warehouse-gauge scale. Latched: carried forward from the previous run and
    # only ratcheted outward when today's stocks exceed a stored extreme. Done
    # after the merge so the ratchet sees the final snapshot set.
    arabica_json["port_peaks"] = build_port_peaks(
        "arabica", arabica_json.get("snapshots") or [],
        (existing_a or {}).get("port_peaks") if merge else None)
    robusta_json["port_peaks"] = build_port_peaks(
        "robusta", robusta_json.get("snapshots") or [],
        (existing_r or {}).get("port_peaks") if merge else None)

    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "certified_stocks_arabica.json").write_text(
            json.dumps(arabica_json, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        (OUT_DIR / "certified_stocks_robusta.json").write_text(
            json.dumps(robusta_json, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"=== wrote {OUT_DIR / 'certified_stocks_arabica.json'}")
        print(f"=== wrote {OUT_DIR / 'certified_stocks_robusta.json'}")

        # News-feed commentary badge (no-op when DATABASE_URL is unset —
        # keeps local backfills from needing a DB). The actual news.json
        # republish happens in workflow 1.4 (export-and-publish) which
        # reads news_feed and writes frontend/public/data/news.json.
        from .news_emit import emit as _emit_news
        try:
            _emit_news(
                arabica_json, robusta_json,
                arabica_source_url=arabica_source_url,
                robusta_source_url=robusta_stock_url,
            )
        except Exception as e:  # noqa: BLE001
            # Commentary is additive — never fail the whole orchestrator
            # over a news-feed write. The JSON snapshots already shipped.
            print(f"[ice-news] FAILED: {e!r} — JSON snapshots already written")

    return {"arabica": arabica_json, "robusta": robusta_json,
            "_sweep_day": sweep_day}


# ── Smoke test ───────────────────────────────────────────────────────────────
# Hits each of the 10 source URLs ONCE against the dates the user verified in
# the probe. If any return non-200 here, the problem is URL/access not data
# volume — not whether ICE happened to publish that day.

_SMOKE_URLS = [
    ("arabica_xls",            F.ARABICA_DAILY_XLS.format(yyyymmdd="20260527")),
    ("stock_report",           F.ROBUSTA_STOCK_REPORT_CSV.format(yyyymmdd="20260527", hhmmss="103021")),
    ("age_allowance",          F.ROBUSTA_AGE_ALLOWANCE_XLSX.format(yyyymmdd="20260430")),
    ("grading_overview",       F.ROBUSTA_GRADING_OVERVIEW_PDF.format(yymmdd="260521")),
    ("gradings",               F.ROBUSTA_GRADINGS_TXT.format(yymmdd="260521", n=1)),
    ("iss_recv_daily",         F.ROBUSTA_ISS_RECV_DAILY.format(yymmdd="260522")),
    ("iss_recv_monthly",       F.ROBUSTA_ISS_RECV_MONTHLY.format(yymmdd="260331")),
    ("grading_appeals",        F.ROBUSTA_GRADING_APPEALS.format(yymmdd="250923", n=1)),
    ("tenders",                F.ROBUSTA_TENDERS.format(yymmdd="260522")),
    ("infested_warrant",       F.ROBUSTA_INFESTED_WARRANT.format(yymmdd="251215")),
]


def smoke() -> int:
    """Hit each of the 10 probe-verified URLs once. Returns # of 200s.

    Every URL above is a FIXED HISTORICAL date, which makes this a check that
    the URL shapes still resolve — not that the feed works today. On
    2026-09-04 it reported 10/10 OK while every current-date request in the
    same minutes returned 403: ICE was serving the archive and refusing recent
    files. Read alone it is a false all-clear, so the live probe below runs
    with it and its result is part of the exit status.
    """
    print("=== SMOKE: probe-verified URLs (expect 10/10 HTTP 200) ===\n")
    ok = 0
    for name, url in _SMOKE_URLS:
        r = _http_get(url)
        if r is not None and r.status_code == 200 and r.content:
            ok += 1
            ctype = r.headers.get("Content-Type", "")[:40]
            print(f"  ✓ {name:18}  HTTP 200  {len(r.content):>10,} B  {ctype}")
    print(f"\n=== SMOKE: {ok}/{len(_SMOKE_URLS)} OK (archive URLs) ===")

    # The question the archive cannot answer: does TODAY's data come through?
    live_day = _biz_days_back(date.today() - timedelta(days=1), 1)[0]
    live_url = F.ARABICA_DAILY_XLS.format(yyyymmdd=live_day.strftime("%Y%m%d"))
    r = _http_get(live_url)
    live_ok = r is not None and r.status_code == 200 and bool(r.content)
    status = "HTTP 200" if live_ok else (f"HTTP {r.status_code}" if r is not None else "no response")
    print(f"\n=== SMOKE (live, {live_day}): {'✓' if live_ok else '✗'} {status} ===")
    if ok == len(_SMOKE_URLS) and not live_ok:
        print("  ! ARCHIVE SERVES, LIVE DOES NOT — the feed is blocked for current\n"
              "    files even though every historical URL resolves. A 10/10 above\n"
              "    is not an all-clear; this is the line that matters.")
    return ok if live_ok else -1


def _cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30,
                    help="business days to look back (default 30)")
    ap.add_argument("--no-write", action="store_true",
                    help="print summary only, don't write JSONs")
    ap.add_argument("--smoke", action="store_true",
                    help="hit probe-verified URLs once each; skip the backfill")
    ap.add_argument("--no-merge", action="store_true",
                    help="overwrite the JSONs instead of merging with existing")
    ap.add_argument("--skip-monthly", action="store_true",
                    help="daily scraper: pull daily feeds only, skip the monthly "
                         "ageing / age-allowance reports")
    ap.add_argument("--only-monthly", action="store_true",
                    help="monthly scraper: pull only the ageing / age-allowance "
                         "reports, skip the daily feeds + robusta sweep")
    args = ap.parse_args()

    if args.skip_monthly and args.only_monthly:
        ap.error("--skip-monthly and --only-monthly are mutually exclusive")

    if args.smoke:
        ok = smoke()
        sys.exit(0 if ok == len(_SMOKE_URLS) else 1)

    out = run(days_back=args.days, write=not args.no_write, merge=not args.no_merge,
              skip_monthly=args.skip_monthly, only_monthly=args.only_monthly)
    print(f"\nSUMMARY: arabica snapshots={len(out['arabica']['snapshots'])} · "
          f"robusta snapshots={len(out['robusta']['snapshots'])} · "
          f"gradings={len(out['robusta']['recent_activity']['gradings'])}")
    # Telemetry last, so it records what the run actually cost.
    _record_run_stats(
        outcome=("aborted_429" if _RUN_STATS["aborted_by_429"]
                 else "aborted_403" if _RUN_STATS["aborted_by_403"]
                 else "completed"),
        sweep_day=out.get("_sweep_day"),
    )
    print(f"WAIT: publicdocs {_RUN_STATS['wait_publicdocs_s']/60:.1f} min · "
          f"marketdata {_RUN_STATS['wait_marketdata_s']/60:.1f} min · "
          f"retry-after {sum(_RUN_STATS['retry_after_waits'])/60:.1f} min "
          f"over {_RUN_STATS['requests']} requests")
    print(f"RATE: {_RUN_STATS['http_429']} x 429 · "
          f"{_RUN_STATS['http_403']} x 403 · "
          f"{len(_RUN_STATS['retry_after_waits'])} Retry-After waits "
          f"({round(sum(_RUN_STATS['retry_after_waits']))}s total) · "
          f"{_RUN_STATS['throttle_bumps']} throttle bumps · "
          f"{_RUN_STATS['sweep_gets']} sweep GETs · "
          f"{_RUN_STATS['http_404']} x 404 · "
          f"{_RUN_STATS['ok_200']} x 200")
    # A 403 skips a section and lets the run finish green. Green must not be
    # the same word as complete, so the skip is announced everywhere a person
    # looks: Telegram now, the ICE research page from ice_run_stats.json, and
    # the data-map run record from run_degradations.json.
    _notify_blocked_sections(only_monthly=args.only_monthly, skip_monthly=args.skip_monthly)
    _publish_degradation()
    # Last, so the telemetry above is printed and recorded before we blow up.
    # Exit 3 rather than 1: the caller retries a transient fault, and a feed
    # refusing every request is not one. Retrying it just pays the whole run
    # twice — run 33854928072 did exactly that, ~25 minutes per attempt.
    try:
        _assert_not_wholly_refused()
    except AllRequestsRefused as e:
        print(f"\n{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    _cli()
