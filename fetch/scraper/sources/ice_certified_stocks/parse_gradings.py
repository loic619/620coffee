"""Robusta gradings (daily) and grading appeals (rare) — same fixed-width text.

Source URLs:
  …/gradings/gradrc_YYMMDD-N.txt
  …/grading_appeals/apprc_YYMMDD-N.txt

Each entry row is: ORIGIN (multi-word) ... PORT(3-letter) ... (CLASS digit + ALLOWANCE  |  "Non Tender") ... LOTS
Plus footer with Total Tenderable / Non-Tenderable / Lots Graded (today + month).
Section headings ("UK LOTS" / "CONTINENTAL LOTS") tag which lot region the
following entries belong to.
"""
from __future__ import annotations

import re

from ._common import parse_ice_date

_PANEL = re.compile(r"Panel Date:\s*(\d{1,2}-[A-Za-z]{3}-\d{4}).*?Panel Time:\s*(\d{2}:\d{2})")
_HEADER_DATE = re.compile(r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+(\d{2}:\d{2})")
_SECTION = re.compile(r"^\s*(UK LOTS|CONTINENTAL LOTS)\s*$", re.IGNORECASE)
# Entry: <origin>  <PORT 3-letter>  ( <class>  <allowance> | "Non Tender" )  <lots>
# Class is 1-4 on the standard ladder (par / -30 / -60 / -90 cts/lb) but ICE
# also grades a premium class "P". Matching digits only silently DROPPED any
# premium row — the whole line failed the regex and never reached the JSON.
_ENTRY = re.compile(
    r"^(?P<origin>.+?)\s{2,}(?P<port>[A-Z]{3})\s+"
    r"(?:(?P<cls>\d+|[Pp])\s+(?P<allow>-?\d+\.\d+)|(?P<nontender>Non\s*Tender))"
    r"\s+(?P<lots>\d+)\s*$"
)
_FOOTER = re.compile(
    r"^(?P<label>Total Tenderable Lots|Total Non-Tenderable Lots|Total Lots Graded)"
    r"\s+(?P<today>\d+)\s+(?P<month>\d+)\s*$"
)


def _parse_class(raw: str | None) -> int | str | None:
    """Grading class: 1-4 as ints, the premium class as the string "P"."""
    if not raw:
        return None
    return "P" if raw.upper() == "P" else int(raw)


def parse_gradings(text: str) -> dict:
    panel_date_iso: str | None = None
    panel_time: str | None = None
    report_dt_iso: str | None = None
    report_time: str | None = None

    section: str | None = None
    entries: list[dict] = []
    footer = {"tenderable_today": 0, "tenderable_month": 0,
              "non_tenderable_today": 0, "non_tenderable_month": 0,
              "lots_graded_today": 0, "lots_graded_month": 0}

    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if panel_date_iso is None:
            m = _PANEL.search(s)
            if m:
                panel_date_iso = parse_ice_date(m.group(1))
                panel_time = m.group(2)
                continue
        if report_dt_iso is None:
            m = _HEADER_DATE.search(s)
            if m:
                report_dt_iso = parse_ice_date(m.group(1))
                report_time = m.group(2)
                # don't `continue` — header line may be alone
        m = _SECTION.match(s)
        if m:
            section = m.group(1).upper()
            continue
        m = _ENTRY.match(s.strip())
        if m:
            entries.append({
                "section":          section,
                "origin":           m.group("origin").strip(),
                "port":             m.group("port"),
                "class":            _parse_class(m.group("cls")),
                "tenderable":       m.group("nontender") is None,
                "allowance_cts_lb": float(m.group("allow")) if m.group("allow") else None,
                "lots":             int(m.group("lots")),
            })
            continue
        m = _FOOTER.match(s.strip())
        if m:
            label = m.group("label")
            today = int(m.group("today")); month = int(m.group("month"))
            if label == "Total Tenderable Lots":
                footer["tenderable_today"] = today; footer["tenderable_month"] = month
            elif label == "Total Non-Tenderable Lots":
                footer["non_tenderable_today"] = today; footer["non_tenderable_month"] = month
            elif label == "Total Lots Graded":
                footer["lots_graded_today"] = today; footer["lots_graded_month"] = month

    return {
        "report_date":   report_dt_iso or panel_date_iso,
        "report_time":   report_time,
        "panel_date":    panel_date_iso,
        "panel_time":    panel_time,
        "entries":       entries,
        "summary":       footer,
    }
