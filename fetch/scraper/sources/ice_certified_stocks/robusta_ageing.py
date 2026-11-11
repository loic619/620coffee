"""Robusta (RC) port x age checks for the month-end age-allowance report.

The data itself is already in ``monthly.age_allowance``: ICE Futures Europe's
month-end Robusta Coffee Age Allowance report, ``valid`` sheet, one bucket per
"Months Since Graded" (1, 2, 3, ...) x port, in metric tonnes. This module does
NOT copy those cells again (the robusta file is already ~1 MB); it emits the
``ageing_cohorts`` block the panel reads beside them: three checks plus the
port-code alias the checks proved.

How this differs from arabica (``arabica_ageing_history.py``), measured on the
31 month-ends on file (2024-04-30 .. 2026-10-30) when this was written:

* The age unit is calendar months since grading, not 30-day buckets.
  Bucket 1 is "graded this calendar month": at every month-end since the
  gradings feed began, bucket 1 at a port sits at or just under the tenderable
  lots graded at that port in that month (e.g. ANT 2026-10: 2,070 vs 2,081).
* Buckets chain. Bucket n at one month-end is larger than bucket n-1 at the
  previous one in only 5 port-cells across 30 consecutive month pairs (three
  by a single lot; ANT bucket 2 in 2026-07 and 2026-09 by 62 and 54 lots), so
  unlike arabica the older bands behave as cohorts. Growth is emitted as
  ``shift_flags`` (a flag, never a failure), as for arabica.
* Port totals do NOT always tie to the daily stock report: 15 port-months
  differ, by -43 to +65 lots, some on the very same date. Cause not
  established (not non-tenderable or suspended lots, which do not match
  either). Emitted as ``port_total_mismatches``, never hidden.
* Port codes: the report wrote ``NEW`` from 2024-12 for the port it called
  ``NOR`` before, and the daily stock report always says ``NOR``. Proved by tie:
  2025-05-30 age report NEW 50 t vs daily NOR 5 lots; 2025-07 bucket 1 NEW 2
  lots vs 2 lots graded at NOR.

Deterministic (no timestamps), so re-running on unchanged inputs changes
nothing in the published file.
"""
from __future__ import annotations

from datetime import date, timedelta

MT_PER_LOT = 10

# Report code -> daily stock-report code. See the module docstring for the tie.
PORT_ALIASES = {"NEW": "NOR"}

# Same window as arabica: ICE dates the report by calendar month-end; on a
# weekend the last daily snapshot is the business day before.
TIE_WINDOW_DAYS = 5


def _canon(port: str) -> str:
    return PORT_ALIASES.get(port, port)


def _table_lots(valid: dict) -> dict:
    """{(port, months_since_graded): lots} for non-zero cells, ports canonical."""
    out: dict = {}
    for b in (valid or {}).get("buckets") or []:
        m = b.get("months_since_graded")
        if not isinstance(m, int):
            continue
        for p, mt in (b.get("by_port") or {}).items():
            lots = int(mt or 0) // MT_PER_LOT
            if lots:
                k = (_canon(p), m)
                out[k] = out.get(k, 0) + lots
    return out


def _graded_tenderable_by_port(gradings: list, month: str, last_day: str) -> dict:
    """Tenderable lots graded per port on dates in calendar `month` (YYYY-MM)
    up to `last_day` inclusive."""
    out: dict = {}
    for g in gradings or []:
        d = g.get("date") or ""
        if d[:7] != month or d > last_day:
            continue
        for e in g.get("entries") or []:
            if not e.get("tenderable"):
                continue
            p = _canon(e.get("port") or "")
            if p:
                out[p] = out.get(p, 0) + int(e.get("lots") or 0)
    return out


def build_robusta_ageing_cohorts(age_allowance: list, gradings: list, snapshots: list) -> dict:
    reports = sorted((a for a in (age_allowance or [])
                      if isinstance(a.get("valid"), dict) and (a["valid"].get("buckets"))),
                     key=lambda a: a["month_end"])
    g_dates = sorted(g["date"] for g in (gradings or []) if g.get("date"))
    coverage_start = g_dates[0] if g_dates else None
    by_date = {s["date"]: s for s in (snapshots or []) if s.get("date")}

    young, ties, flags = [], [], []
    prev_tab = None
    prev_me = None
    for a in reports:
        me = a["month_end"]
        me_d = date.fromisoformat(me)
        last_day = max(me, a.get("report_date") or me)
        tab = _table_lots(a["valid"])

        # (i) bucket 1 vs tenderable lots graded at the port this month. Only
        # months whose first day the gradings feed covers.
        if coverage_start and coverage_start <= me[:8] + "01":
            graded = _graded_tenderable_by_port(gradings, me[:7], last_day)
            b1 = {p: v for (p, m), v in tab.items() if m == 1}
            for port in sorted(set(graded) | set(b1)):
                g, b = graded.get(port, 0), b1.get(port, 0)
                young.append({"month_end": me, "port": port, "bucket1_lots": b,
                              "graded_tenderable_lots": g, "shortfall_lots": g - b,
                              "ok": b <= g})

        # (ii) port totals vs the daily snapshot of the same date.
        lo = (me_d - timedelta(days=TIE_WINDOW_DAYS)).isoformat()
        near = [d for d in by_date if lo < d <= last_day]
        if near:
            snap = by_date[max(near)]
            daily: dict = {}
            for p, v in (snap.get("by_port_lots") or {}).items():
                daily[_canon(p)] = daily.get(_canon(p), 0) + int(v or 0)
            rep: dict = {}
            for (p, _m), v in tab.items():
                rep[p] = rep.get(p, 0) + v
            for port in sorted({p for p, v in rep.items() if v} | {p for p, v in daily.items() if v}):
                r, dl = rep.get(port, 0), daily.get(port, 0)
                if r != dl:
                    ties.append({"month_end": me, "daily_date": snap["date"], "port": port,
                                 "report_lots": r, "daily_lots": dl, "diff": r - dl})

        # (iii) shift test: a cohort can only shrink as it ages one month.
        if prev_tab is not None:
            for (port, m), v in sorted(tab.items()):
                src = prev_tab.get((port, m - 1), 0)
                if m >= 2 and v > src:
                    flags.append({"from": prev_me, "to": me, "port": port,
                                  "months_since_graded": m, "lots": v,
                                  "prior_lots_one_month_younger": src})
        prev_tab, prev_me = tab, me

    return {
        "method": ("Port x months-since-graded snapshots of ICE Futures Europe's month-end Robusta "
                   "Age Allowance report (valid stock sheet), read from monthly.age_allowance; this "
                   "block carries only the checks. Bucket 1 (graded this calendar month) is checked "
                   "against tenderable lots graded at the port that month. Shift flags list cells "
                   "larger than the bucket one month younger at the previous report."),
        "source": "monthly.age_allowance[].valid",
        "unit": "lots",
        "months": [a["month_end"] for a in reports],
        "port_aliases": dict(PORT_ALIASES),
        "gradings_coverage_start": coverage_start,
        "youngest_bucket_check": young,
        "port_total_mismatches": ties,
        "shift_flags": flags,
    }
