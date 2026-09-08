"""Robusta tenders (market delivery) report — per-origin originals/retenders.

Source URL: …/tenders/tendrc_YYMMDD.txt

Format (sample):
    ICE EU Guardian - MARKET DELIVERY REPORT - 22-May-2026 RC  22-May-2026 16:01
    Commodity : RC Delivery Period: May-2026 Business Date : 22-May-2026
    Original Tender Date : 22-May-2026      Today          Over Month
         Angola
         Originals:                            0                   1
         Retenders:                            0                   0
         ...
    Totals for 22-May-2026
         Originals:                          155                1346
         Retenders:                            0                   0
    GRAND TOTALS:
         Originals:                          155                1346
         Retenders:                            0                   0
         Originals Rejected:                   0                   0
         ...
"""
from __future__ import annotations

import re

from ._common import parse_ice_date, parse_ice_month

_REPORT_DATE  = re.compile(r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+\d{2}:\d{2}")
_DELIV_PERIOD = re.compile(r"Delivery\s+Period\s*:\s*([A-Za-z]{3}-\d{4})")
_BUSINESS_DT  = re.compile(r"Business\s+Date\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{4})")

# Origin line: indented, just a label (no numbers).
_ORIGIN_LINE = re.compile(r"^\s{2,}([A-Za-z][\w '\-/]+?)\s*$")
# Numeric line: "Label:  today  month" — labels include Originals, Retenders,
# Originals Rejected, Retender Substitutions, etc.
_NUMERIC = re.compile(r"^\s*([A-Za-z][\w '\-/]+?):\s+(\d+)\s+(\d+)\s*$")
_TOTALS_BANNER = re.compile(r"Totals\s+for\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", re.IGNORECASE)
_GRAND_BANNER  = re.compile(r"GRAND\s+TOTALS", re.IGNORECASE)


def _label_key(label: str) -> str:
    return label.strip().lower().replace("'", "").replace("-", "_").replace(" ", "_")


def parse_tenders(text: str) -> dict:
    report_date: str | None = None
    delivery_period: str | None = None
    business_date: str | None = None
    by_origin: list[dict] = []
    totals_today: dict[str, int] = {}
    grand_totals: dict[str, int] = {}

    cur_origin: dict | None = None
    section: str = "by_origin"   # by_origin → totals → grand_totals

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if report_date is None:
            m = _REPORT_DATE.search(line)
            if m:
                report_date = parse_ice_date(m.group(1))
        if delivery_period is None:
            m = _DELIV_PERIOD.search(line)
            if m:
                delivery_period = parse_ice_month(m.group(1))
        if business_date is None:
            m = _BUSINESS_DT.search(line)
            if m:
                business_date = parse_ice_date(m.group(1))

        if _TOTALS_BANNER.search(line):
            section = "totals"
            cur_origin = None
            continue
        if _GRAND_BANNER.search(line):
            section = "grand_totals"
            cur_origin = None
            continue

        # Numeric "Label: today month" line.
        m = _NUMERIC.match(line)
        if m:
            label = m.group(1); today = int(m.group(2)); month = int(m.group(3))
            key = _label_key(label)
            if section == "by_origin" and cur_origin is not None:
                cur_origin[f"{key}_today"] = today
                cur_origin[f"{key}_month"] = month
            elif section == "totals":
                totals_today[key] = today
                totals_today[f"{key}_month"] = month
            elif section == "grand_totals":
                grand_totals[key] = today
                grand_totals[f"{key}_month"] = month
            continue

        # Origin label line (only meaningful in by_origin section).
        if section == "by_origin":
            m = _ORIGIN_LINE.match(raw)
            if m:
                cur_origin = {"origin": m.group(1).strip()}
                by_origin.append(cur_origin)

    return {
        "report_date":     report_date,
        "delivery_period": delivery_period,
        "business_date":   business_date,
        "by_origin":       by_origin,
        "totals_today":    totals_today,
        "grand_totals":    grand_totals,
    }
