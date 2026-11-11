"""Arabica ageing-report history and the port x age snapshot view built on it.

ICE publishes the KC ageing report (``coffee_aging_YYYYMMDD.xls``) once a month:
port x 30-day age bucket, no origin. The pipeline used to keep only the newest
one in ``ageing_report`` and overwrite it every month, so the history survived
only in git. This module is the fix:

* ``validate_ageing_report`` - the single gate every report passes before it is
  kept. An empty report (grand_total 0, as ICE served for 2026-05-31) is
  rejected with a reason; it never enters the history as data.
* ``append_ageing_history`` - idempotent upsert keyed by ``month_end``. Same
  content again is a no-op (no churn in the published file); a revised report
  for a month replaces the old one and records what it superseded.
* ``build_ageing_cohorts`` - the ``ageing_cohorts`` block: port x age-bucket
  SNAPSHOTS per month-end, with an imputed origin mix for buckets whose grading
  window is covered by the daily gradings, plus three checks.

Older buckets are deliberately NOT chained across months into tracked cohorts.
Measured on the four month-ends recoverable from git (handoff
``docs/handoff/2026-10-29__demand__port-cohort-feasibility.md``, example B),
shifting a bucket by 30 days between consecutive reports makes stock GROW, which
a cohort cannot do; ICE's age definition for old stock is unresolved. Only the
youngest bucket ties to the daily gradings (example A), so that is the one
invariant checked; the shift test is emitted as a flag, never a failure.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# Shift test: buckets at least this old are compared (the youngest bucket,
# 0000-0120, lumps four months and is checked against gradings instead).
SHIFT_MIN_AGE_DAYS = 151
SHIFT_DAYS = 30

# Documented, not hidden. Over the 22 month-ends that have a daily snapshot
# (2025-05-30 .. 2026-09-30) port totals tie exactly except these two; causes
# unknown. Any other port-total gap shows as an unexplained mismatch.
KNOWN_PORT_TIE_EXCEPTIONS = {
    "2026-06-30": "MIAMI: age report 500 bags below daily total; unexplained",
    "2026-03-31": "NY: age report 250 bags above daily total; unexplained",
}

# Daily snapshots before ~2026-05 carry the legacy port codes; the ageing
# report always uses the current ones. Measured: the pairs offset exactly.
PORT_ALIASES = {"HAM": "HA/BR", "MIA": "MIAMI", "NOR": "NOLA", "NYK": "NY", "VIR": "VA"}


def _canon_ports(by_port: dict) -> dict:
    out: dict = {}
    for p, v in (by_port or {}).items():
        k = PORT_ALIASES.get(p, p)
        out[k] = out.get(k, 0) + int(v or 0)
    return out


_BUCKET_RE = re.compile(r"(\d+)\D+(\d+)")


def parse_bucket(label: str) -> tuple[int, int | None] | None:
    """'0121 to 0150' -> (121, 150); 'Over 1000' -> (1000, None); junk -> None."""
    s = (label or "").strip()
    m = _BUCKET_RE.search(s)
    if m:
        return int(m.group(1)), int(m.group(2))
    one = re.search(r"\d+", s)
    if one:
        return int(one.group(0)), None
    return None


def _detail_total(age_detail: dict) -> int:
    return sum(int(r.get("bags") or 0) for rows in (age_detail or {}).values() for r in rows or [])


def validate_ageing_report(report) -> tuple[bool, str]:
    """(True, '') for a report worth keeping, else (False, reason)."""
    if not isinstance(report, dict):
        return False, "not an object"
    me = report.get("month_end")
    try:
        date.fromisoformat(str(me))
    except ValueError:
        return False, f"month_end missing or not ISO: {me!r}"
    gt = report.get("grand_total")
    if not isinstance(gt, (int, float)) or gt <= 0:
        return False, f"empty report: grand_total {gt!r}"
    detail = report.get("age_detail")
    if not isinstance(detail, dict) or not detail:
        return False, "age_detail empty"
    dt = _detail_total(detail)
    if dt != gt:
        return False, f"age_detail sums to {dt}, grand_total says {gt}"
    for port, rows in detail.items():
        for r in rows or []:
            if parse_bucket(r.get("age_bucket", "")) is None:
                return False, f"unreadable age bucket {r.get('age_bucket')!r} at {port}"
            if int(r.get("bags") or 0) < 0:
                return False, f"negative bags at {port}"
    return True, ""


def history_entry(report: dict, source: str, provenance: dict) -> dict:
    """The compact record kept per month-end: what the panel and checks need."""
    return {
        "month_end": report["month_end"],
        "source": source,
        "source_url": report.get("source_url"),
        "grand_total": int(report["grand_total"]),
        "by_port": report.get("by_port") or {
            p: sum(int(r.get("bags") or 0) for r in rows) for p, rows in report["age_detail"].items()},
        "age_detail": report["age_detail"],
        "provenance": provenance,
    }


def append_ageing_history(history: list | None, report, source: str,
                          provenance: dict | None = None) -> tuple[list, str]:
    """Upsert ``report`` into ``history`` by month_end. Returns (history, action).

    action is one of 'appended', 'unchanged', 'revised', 'rejected: <reason>',
    'none'. An invalid report never changes the history.
    """
    hist = list(history or [])
    if not report:
        return hist, "none"
    ok, why = validate_ageing_report(report)
    if not ok:
        return hist, f"rejected: {why}"
    by_me = {h["month_end"]: i for i, h in enumerate(hist)}
    me = report["month_end"]
    if me in by_me:
        cur = hist[by_me[me]]
        if cur.get("grand_total") == report["grand_total"] and cur.get("age_detail") == report["age_detail"]:
            return hist, "unchanged"
        entry = history_entry(report, source, provenance or {})
        entry["provenance"] = {**entry["provenance"],
                               "supersedes": {"grand_total": cur.get("grand_total"),
                                              "source": cur.get("source")}}
        hist[by_me[me]] = entry
        action = "revised"
    else:
        hist.append(history_entry(report, source, provenance or {}))
        action = "appended"
    hist.sort(key=lambda h: h["month_end"])
    return hist, action


# ── ageing_cohorts ───────────────────────────────────────────────────────────

def _passed_by_port_origin(snapshots: list, start: str, end: str, port: str) -> dict:
    """Bags passed at `port` per origin over snapshot dates in (start, end]."""
    out: dict = {}
    for s in snapshots:
        if not (start < s.get("date", "") <= end):
            continue
        for origin, o in (s.get("passed_by_origin") or {}).items():
            b = _canon_ports((o or {}).get("by_port")).get(port, 0)
            if b:
                out[origin] = out.get(origin, 0) + b
    return out


def _youngest(rows: list) -> tuple[str | None, int]:
    best = None
    for r in rows or []:
        b = parse_bucket(r.get("age_bucket", ""))
        if b and (best is None or b[0] < best[0][0]):
            best = (b, r)
    if best is None or best[0][0] != 0:
        return None, 0
    return best[1]["age_bucket"], int(best[1].get("bags") or 0)


def bucket_growth(prev_detail: dict, cur_detail: dict, shift_days: int = SHIFT_DAYS,
                  min_age: int = SHIFT_MIN_AGE_DAYS) -> dict:
    """Per port, bags that APPEAR in an old bucket beyond what the bucket
    `shift_days` younger held a report earlier. A tracked cohort can only
    shrink, so any positive number means the buckets do not chain at this shift.
    """
    out: dict = {}
    for port in sorted(set(prev_detail or {}) | set(cur_detail or {})):
        prev = {}
        for r in (prev_detail or {}).get(port) or []:
            b = parse_bucket(r.get("age_bucket", ""))
            if b:
                prev[b] = prev.get(b, 0) + int(r.get("bags") or 0)
        growth = 0
        for r in (cur_detail or {}).get(port) or []:
            b = parse_bucket(r.get("age_bucket", ""))
            if not b or b[0] < min_age:
                continue
            src = (b[0] - shift_days, None if b[1] is None else b[1] - shift_days)
            growth += max(0, int(r.get("bags") or 0) - prev.get(src, 0))
        out[port] = growth
    return out


def build_ageing_cohorts(history: list, snapshots: list) -> dict:
    """Port x age-bucket snapshot rows plus the invariant checks. Deterministic
    (no timestamps), so re-running on unchanged inputs changes nothing."""
    hist = sorted(history or [], key=lambda h: h["month_end"])
    snaps = sorted(snapshots or [], key=lambda s: s.get("date", ""))
    by_date = {s["date"]: s for s in snaps if s.get("date")}
    covered = [s["date"] for s in snaps if s.get("passed_by_origin")]
    coverage_start = covered[0] if covered else None

    rows, young, ties, flags = [], [], [], []
    prev = None
    for h in hist:
        me = h["month_end"]
        me_d = date.fromisoformat(me)
        for port in sorted(h["age_detail"]):
            for r in h["age_detail"][port]:
                lo, hi = parse_bucket(r["age_bucket"])
                mix = None
                if hi is not None and coverage_start:
                    w_start = (me_d - timedelta(days=hi + 1)).isoformat()
                    w_end = (me_d - timedelta(days=lo)).isoformat()
                    if w_start >= coverage_start:
                        got = _passed_by_port_origin(snaps, w_start, w_end, port)
                        tot = sum(got.values())
                        if tot:
                            mix = {o: round(b / tot, 4) for o, b in sorted(got.items())}
                rows.append({"month_end": me, "port": port, "age_bucket": r["age_bucket"],
                             "age_days_lo": lo, "age_days_hi": hi, "bags": int(r["bags"]),
                             "origin_mix_imputed": mix, "source": h.get("source")})

        # (ii) port totals vs the daily snapshot of the same date
        # ICE dates the file by calendar month-end; on a weekend the last
        # daily snapshot is the business day before.
        on_or_before = [d for d in by_date if d <= me and d > (me_d - timedelta(days=5)).isoformat()]
        snap = by_date[max(on_or_before)] if on_or_before else None
        if snap:
            daily = _canon_ports(snap.get("by_port"))
            for port in sorted(set(h["by_port"]) | {p for p, v in daily.items() if v}):
                rep = int(h["by_port"].get(port) or 0)
                day = daily.get(port, 0)
                if rep != day:
                    ties.append({"month_end": me, "daily_date": snap["date"], "port": port, "report_bags": rep, "daily_bags": day,
                                 "diff": rep - day, "known_exception": KNOWN_PORT_TIE_EXCEPTIONS.get(me)})

        # (i) youngest bucket vs passed gradings since the previous report
        if prev is not None and coverage_start and prev["month_end"] >= coverage_start:
            for port in sorted(set(h["age_detail"]) | set(prev["age_detail"])):
                label, yb = _youngest(h["age_detail"].get(port))
                _, prior = _youngest(prev["age_detail"].get(port))
                passed = sum(_passed_by_port_origin(snaps, prev["month_end"], me, port).values())
                if not (yb or passed):
                    continue
                residual = yb - passed
                young.append({"month_end": me, "port": port, "youngest_bucket": label,
                              "youngest_bags": yb, "passed_since_prior_report": passed,
                              "residual": residual, "prior_youngest_bags": prior,
                              "ok": 0 <= residual <= prior})

            # (iii) shift test: flag only for arabica (known violations)
            for port, g in bucket_growth(prev["age_detail"], h["age_detail"]).items():
                if g > 0:
                    flags.append({"from": prev["month_end"], "to": me, "port": port,
                                  "growth_bags_shift_30d": g})
        prev = h

    return {
        "method": ("Port x age-bucket snapshots of the ICE KC ageing report, one per month-end. "
                   "NOT tracked cohorts: older buckets do not chain month to month (shift test "
                   "flags). Youngest bucket checked against daily passed gradings. Origin mix is "
                   "imputed from daily passed gradings at the port over the bucket's grading window, "
                   "null where that window predates the gradings feed."),
        "months": [h["month_end"] for h in hist],
        "gradings_coverage_start": coverage_start,
        "rows": rows,
        "youngest_bucket_check": young,
        "port_total_mismatches": ties,
        "shift_flags": flags,
    }
