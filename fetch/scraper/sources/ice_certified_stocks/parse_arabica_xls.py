"""Arabica daily certified-stock report (.xls — OLE2 binary, parsed via xlrd).

Source: …/coffee_cert_stock_YYYYMMDD.xls — single sheet, 5 sections:
  • TOTAL BAGS CERTIFIED       (origin × 8 ports: ANT/BAR/HA-BR/HOU/MIAMI/NOLA/NY/VA)
  • TRANSITION BAGS CERTIFIED  (origin × 3 ports: ANT/BAR/HA-BR)
  • TODAY'S GRADING SUMMARY    (passed/failed bags — text or table)
  • Pending Grading Report     (origin × 8 ports)
  • Flagged for Rebagging      (origin × 8 ports, often empty)

Each origin row is bucketed into its ICE C-contract group (Group 0–4) via
ice_arabica_groups for the by_group rollups the certified-stocks panel needs.
"""
from __future__ import annotations

import re
from datetime import datetime

import xlrd

from ..ice_arabica_groups import group_of

_AS_OF = re.compile(r"As of:\s*([A-Za-z]+ \d{1,2}, \d{4})")
_BAG_COUNT = re.compile(r"^\s*(\d[\d,]*|No)\s+Bags\s+(Passed|Failed)", re.IGNORECASE)
# Action-day (June-2026+) grading layout uses matrix sub-section headers
# instead of the old "N Bags Passed Today" one-liner.
_PASSED_HDR = re.compile(r"bags\s+passed\s+grading", re.IGNORECASE)
_FAILED_HDR = re.compile(r"bags\s+failed\s+grading", re.IGNORECASE)

_SECTION_TITLES = (
    ("total_certified", "TOTAL BAGS CERTIFIED"),
    ("transition",      "TRANSITION BAGS CERTIFIED"),
    ("grading_summary", "TODAY'S GRADING SUMMARY"),
    ("pending_grading", "PENDING GRADING REPORT"),
    ("rebagging",       "FLAGGED FOR REBAGGING"),
)


def _to_int(v) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _find_sections(sheet) -> dict[str, int]:
    """First occurrence of each section title (file body later mentions a
    "Transition Bags Certified data is..." explainer row that would otherwise
    overwrite the real section anchor)."""
    found: dict[str, int] = {}
    for r in range(sheet.nrows):
        cell = str(sheet.cell_value(r, 0)).strip().upper()
        for key, title in _SECTION_TITLES:
            if key in found:
                continue
            if cell.startswith(title):
                found[key] = r
                break
    return found


def _find_header_row(sheet, start_row: int, max_skip: int = 6) -> int | None:
    """First row after `start_row` whose row has 'Total' in some column (the
    "...|ANT|BAR|...|Total" header). Returns None if not found within max_skip."""
    for r in range(start_row + 1, min(start_row + 1 + max_skip, sheet.nrows)):
        cells = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
        if any(c.lower() == "total" for c in cells):
            return r
    return None


def _parse_matrix(sheet, section_row: int) -> dict:
    """Parse an origin × port matrix section. Returns a rich roll-up:
        {ports, by_origin, by_port, by_group, grand_total}
    """
    header_row = _find_header_row(sheet, section_row)
    if header_row is None:
        return {"ports": [], "by_origin": {}, "by_port": {}, "by_group": {}, "grand_total": 0}

    headers = [str(sheet.cell_value(header_row, c)).strip() for c in range(sheet.ncols)]
    port_cols: list[tuple[int, str]] = []   # (col_idx, port_code)
    total_col: int | None = None
    for c, h in enumerate(headers):
        if c == 0 or not h:
            continue
        if h.lower() == "total":
            total_col = c
        else:
            port_cols.append((c, h))

    by_origin: dict[str, dict] = {}
    r = header_row + 1
    while r < sheet.nrows:
        label = str(sheet.cell_value(r, 0)).strip()
        low = label.lower()
        if not label:
            r += 1; continue
        if low.startswith("total in bags") or low.startswith("total"):
            break
        ports = {p: _to_int(sheet.cell_value(r, c)) for c, p in port_cols}
        origin_total = _to_int(sheet.cell_value(r, total_col)) if total_col is not None else sum(ports.values())
        grp = group_of(label) or "Unknown"
        by_origin[label] = {"by_port": ports, "group": grp, "total": origin_total}
        r += 1

    by_port = {p: sum(o["by_port"].get(p, 0) for o in by_origin.values()) for _, p in port_cols}
    by_group: dict[str, int] = {}
    for o in by_origin.values():
        by_group[o["group"]] = by_group.get(o["group"], 0) + o["total"]
    grand_total = sum(o["total"] for o in by_origin.values())

    return {
        "ports":       [p for _, p in port_cols],
        "by_origin":   by_origin,
        "by_port":     by_port,
        "by_group":    by_group,
        "grand_total": grand_total,
    }


def _parse_grading_summary(sheet, section_row: int) -> dict:
    """Parse "TODAY'S GRADING SUMMARY" in BOTH layouts:

    • No-action / legacy text:  "No Bags Passed Today" / "825 Bags Passed Today"
      (a leading "No" or integer before "Bags Passed/Failed"). No origin detail.
    • Action-day matrix (ICE format from June 2026): a "BAGS PASSED GRADING"
      header followed by an origin × port table ending in a "Total in Bags"
      row; then "BAGS FAILED GRADING" with its own table. We capture the FULL
      matrix for each (by_origin × by_port), not just the grand total, so the
      certified-stocks model can attribute gradings to (origin, port) cohorts.
    """
    passed = failed = 0
    passed_text = failed_text = None
    # Per-(origin, port) grading matrices — only present on action-day (matrix)
    # reports; None on legacy/no-action days. Shape mirrors _parse_matrix:
    #   {ports, by_origin: {origin: {by_port, group, total}}, by_port, by_group, grand_total}
    passed_detail: dict | None = None
    failed_detail: dict | None = None
    for r in range(section_row + 1, min(section_row + 40, sheet.nrows)):
        text = str(sheet.cell_value(r, 0)).strip()
        low = text.lower()
        if "pending grading" in low or "flagged for rebagging" in low:
            break
        if not text:
            continue
        # Matrix sub-section headers (new format) — parse the whole table.
        if _PASSED_HDR.search(low):
            passed_detail = _parse_matrix(sheet, r)
            passed = passed_detail["grand_total"]
            passed_text = f"{passed} bags passed (matrix)"
            continue
        if _FAILED_HDR.search(low):
            failed_detail = _parse_matrix(sheet, r)
            failed = failed_detail["grand_total"]
            failed_text = f"{failed} bags failed (matrix)"
            continue
        # Legacy one-liner: "No|123 Bags Passed/Failed …".
        m = _BAG_COUNT.match(text)
        if m:
            token, kind = m.group(1), m.group(2).lower()
            n = 0 if token.lower() == "no" else int(token.replace(",", ""))
            if kind == "passed":
                passed = n; passed_text = text
            else:
                failed = n; failed_text = text
    return {
        "passed_today_bags": passed,
        "failed_today_bags": failed,
        "passed_text":       passed_text,
        "failed_text":       failed_text,
        "passed_detail":     passed_detail,
        "failed_detail":     failed_detail,
    }


def parse_arabica_xls(content: bytes) -> dict:
    wb = xlrd.open_workbook(file_contents=content)
    sheet = wb.sheet_by_index(0)

    # Report date — "As of: May 27, 2026  1:34:30PM" lives in the first ~4 rows.
    report_date_iso: str | None = None
    for r in range(min(5, sheet.nrows)):
        v = str(sheet.cell_value(r, 0))
        m = _AS_OF.search(v)
        if m:
            # ICE switched to abbreviated month names ("Jun 1, 2026") in
            # mid-2026; accept both abbreviated (%b) and full (%B).
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    report_date_iso = datetime.strptime(m.group(1), fmt).date().isoformat()
                    break
                except ValueError:
                    continue
            break

    sections = _find_sections(sheet)
    out = {
        "report_date":    report_date_iso,
        "total_certified": _parse_matrix(sheet, sections["total_certified"]) if "total_certified" in sections else None,
        "transition":      _parse_matrix(sheet, sections["transition"])      if "transition"      in sections else None,
        "pending_grading": _parse_matrix(sheet, sections["pending_grading"]) if "pending_grading" in sections else None,
        "rebagging":       _parse_matrix(sheet, sections["rebagging"])      if "rebagging"      in sections else None,
        "grading_today":   _parse_grading_summary(sheet, sections["grading_summary"]) if "grading_summary" in sections else None,
    }
    return out
