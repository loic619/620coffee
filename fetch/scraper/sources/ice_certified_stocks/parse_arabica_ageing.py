"""Arabica monthly ageing report (.xls — OLE2 binary, xlrd).

Source: https://www.ice.com/publicdocs/futures_us_reports/coffee/coffee_aging_{YYYYMMDD}.xls
Published on the last business day of each calendar month. The URL date is
the month-end date the report covers.

The live ICE file is laid out as **age (in days) × port** — a leftmost "Days"
column of day-range buckets ("0000 to 0120", "0121 to 0150", …) and one column
per delivery port (ANTWERP, HAMBURG / BREMEN, HOUSTON, MIAMI, NEW ORLEANS,
NEW YORK, …) plus a Total. There is NO origin breakdown in this report.

For forward-compatibility the parser still recognises an older / hypothetical
origin × band layout (leftmost "Origin"/"Coffee" column). It tries the origin
layout first; if that yields nothing it falls back to the port layout.

Output:
    {
      "month_end":       "YYYY-MM-DD",
      "source_url":      "...",
      # origin layout (empty for the live port-layout file):
      "by_origin_band":  {origin: {band: bags}},
      "origins":         [str, ...],
      # port layout (populated for the live file):
      "by_port_band":    {port_code: {band: bags}},   # 0Y / 1Y / 2Y / 3Y / >4Y
      "by_port":         {port_code: bags},
      "age_detail":      {port_code: [{age_bucket, bags}]},  # raw day-range rows
      "ports":           [port_code, ...],
      # common:
      "by_band":         {band: bags},                # roll-up across the rows
      "grand_total":     int,
      "parser_notes":    [str, ...],
    }
"""
from __future__ import annotations

import re
from datetime import date

import xlrd

# Canonical year-band labels — match the frontend YEAR_BAND_ORDER.
_BAND_ORDER = ("0Y", "1Y", "2Y", "3Y", ">4Y")

# Delivery-port long names (as they appear in the ageing report header) →
# canonical KC port codes the rest of the pipeline / frontend use.
_PORT_NAME_TO_CODE = {
    "antwerp":            "ANT",
    "barcelona":          "BAR",
    "hamburg / bremen":   "HA/BR",
    "hamburg/bremen":     "HA/BR",
    "hamburg":            "HA/BR",
    "houston":            "HOU",
    "miami":              "MIAMI",
    "new orleans":        "NOLA",
    "new york":           "NY",
    "virginia":           "VA",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _band_from_header(header: str) -> str | None:
    """Map a column header to a year-band label, or None if not an age column."""
    h = _norm(header)
    if not h:
        return None
    # Year-range patterns
    if re.match(r"^0\s*[-–to]\s*1\s*y", h) or "0-1 year" in h or "first year" in h:
        return "0Y"
    if re.match(r"^1\s*[-–to]\s*2\s*y", h) or "1-2 year" in h:
        return "1Y"
    if re.match(r"^2\s*[-–to]\s*3\s*y", h) or "2-3 year" in h:
        return "2Y"
    if re.match(r"^3\s*[-–to]\s*4\s*y", h) or "3-4 year" in h:
        return "3Y"
    if re.match(r"^(over|>|4\+)\s*4\s*y", h) or "over 4" in h or "4+ year" in h:
        return ">4Y"
    # Day-range patterns: 0-365, 366-730, 731-1095, 1096-1460, 1461+
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)", h)
    if m:
        return _band_from_upper_days(int(m[2]))
    m = re.match(r"^(\d+)\s*\+", h)
    if m and int(m[1]) >= 1461:
        return ">4Y"
    return None


def _band_from_upper_days(upper: int) -> str:
    if upper <= 365:    return "0Y"
    if upper <= 730:    return "1Y"
    if upper <= 1095:   return "2Y"
    if upper <= 1460:   return "3Y"
    return ">4Y"


def _band_from_day_bucket(label: str) -> str | None:
    """Map a day-range *row* label ('0000 to 0120', '1461 and over') to a band.
    Uses the bucket's UPPER bound so a 0361-0390 bucket lands in 0Y (<1yr)."""
    h = _norm(label)
    m = re.match(r"^(\d+)\s*(?:to|[-–])\s*(\d+)", h)
    if m:
        return _band_from_upper_days(int(m[2]))
    m = re.match(r"^(\d+)\s*(?:\+|and over|and above|or more)", h)
    if m:
        return _band_from_upper_days(int(m[1]) + 1)
    return None


def _to_int(v) -> int:
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _looks_like_origin_header(s: str) -> bool:
    return _norm(s) in {"origin", "coffee", "country", "country of origin"}


def _looks_like_days_header(s: str) -> bool:
    h = _norm(s)
    return h in {"days", "day", "age", "age in days", "day range", "days range", "ageing", "aging"}


def _blank_result(month_end: date, source_url: str, notes: list[str]) -> dict:
    return {
        "month_end":      month_end.isoformat(),
        "source_url":     source_url,
        "by_origin_band": {},
        "origins":        [],
        "by_port_band":   {},
        "by_port":        {},
        "age_detail":     {},
        "ports":          [],
        "by_band":        {b: 0 for b in _BAND_ORDER},
        "grand_total":    0,
        "parser_notes":   notes,
    }


def _parse_origin_layout(sheet, out: dict, notes: list[str]) -> bool:
    """Old/hypothetical layout: leftmost Origin/Coffee column × band columns.
    Returns True if it parsed at least one origin."""
    header_row = -1
    band_cols: list[tuple[int, str]] = []
    origin_col = 0
    for r in range(min(sheet.nrows, 40)):
        for c in range(min(sheet.ncols, 6)):
            if _looks_like_origin_header(str(sheet.cell_value(r, c))):
                cols = [(cc, b) for cc in range(c + 1, sheet.ncols)
                        if (b := _band_from_header(str(sheet.cell_value(r, cc))))]
                if cols:
                    header_row, origin_col, band_cols = r, c, cols
                    break
        if header_row >= 0:
            break
    if header_row < 0:
        return False

    seen: list[str] = []
    for r in range(header_row + 1, sheet.nrows):
        origin = str(sheet.cell_value(r, origin_col)).strip()
        if not origin or origin.lower().startswith(("total", "grand", "subtotal")):
            continue
        per_band: dict[str, int] = {}
        row_total = 0
        for col, band in band_cols:
            bags = _to_int(sheet.cell_value(r, col))
            if bags <= 0:
                continue
            per_band[band] = per_band.get(band, 0) + bags
            row_total += bags
        if row_total <= 0:
            continue
        seen.append(origin)
        out["by_origin_band"][origin] = per_band
        for b, v in per_band.items():
            out["by_band"][b] += v
        out["grand_total"] += row_total
    out["origins"] = seen
    if seen:
        notes.append(f"origin layout: {len(seen)} origins × {len({b for _, b in band_cols})} bands "
                     f"(grand_total={out['grand_total']:,} bags)")
    return bool(seen)


def _parse_port_layout(sheet, out: dict, notes: list[str]) -> bool:
    """Live ICE layout: leftmost 'Days' column of day-range buckets × port
    columns. Returns True if it parsed at least one port."""
    header_row = -1
    port_cols: list[tuple[int, str]] = []   # (col_idx, port_code)
    days_col = 0
    for r in range(min(sheet.nrows, 40)):
        for c in range(min(sheet.ncols, 4)):
            if _looks_like_days_header(str(sheet.cell_value(r, c))):
                cols = [(cc, code) for cc in range(c + 1, sheet.ncols)
                        if (code := _PORT_NAME_TO_CODE.get(_norm(str(sheet.cell_value(r, cc)))))]
                if cols:
                    header_row, days_col, port_cols = r, c, cols
                    break
        if header_row >= 0:
            break
    if header_row < 0:
        return False

    age_detail: dict[str, list[dict]] = {code: [] for _, code in port_cols}
    for r in range(header_row + 1, sheet.nrows):
        label = str(sheet.cell_value(r, days_col)).strip()
        if not label or label.lower().startswith(("total", "grand", "subtotal")):
            continue
        band = _band_from_day_bucket(label)
        if band is None:
            continue
        for col, code in port_cols:
            bags = _to_int(sheet.cell_value(r, col))
            if bags <= 0:
                continue
            out["by_port_band"].setdefault(code, {})
            out["by_port_band"][code][band] = out["by_port_band"][code].get(band, 0) + bags
            out["by_port"][code] = out["by_port"].get(code, 0) + bags
            out["by_band"][band] += bags
            out["grand_total"] += bags
            age_detail[code].append({"age_bucket": label, "bags": bags})

    out["ports"] = [code for _, code in port_cols if out["by_port"].get(code)]
    out["age_detail"] = {code: rows for code, rows in age_detail.items() if rows}
    if out["grand_total"] > 0:
        notes.append(f"port layout: {len(out['ports'])} ports × day-buckets "
                     f"(grand_total={out['grand_total']:,} bags)")
    return out["grand_total"] > 0


def parse_ageing_sheet(sheet, source_url: str, month_end: date) -> dict:
    """Same shape as parse_arabica_ageing, but takes any object with
    `nrows`, `ncols`, `cell_value(r, c)` — lets tests build a stub without
    needing xlwt to synthesise a real .xls binary."""
    notes: list[str] = []
    out = _blank_result(month_end, source_url, notes)
    # Origin layout first (forward-compat), then the live port layout.
    if _parse_origin_layout(sheet, out, notes):
        return out
    if _parse_port_layout(sheet, out, notes):
        return out
    notes.append("no header row matched (looked for Origin/Coffee or Days + port/band columns)")
    return out


def parse_arabica_ageing(xls_bytes: bytes, source_url: str, month_end: date) -> dict:
    try:
        book = xlrd.open_workbook(file_contents=xls_bytes)
    except Exception as e:  # noqa: BLE001
        return _blank_result(month_end, source_url, [f"xlrd open failed: {e!r}"])
    return parse_ageing_sheet(book.sheet_by_index(0), source_url, month_end)


def fetch_url_for(month_end: date) -> str:
    return f"https://www.ice.com/publicdocs/futures_us_reports/coffee/coffee_aging_{month_end:%Y%m%d}.xls"
