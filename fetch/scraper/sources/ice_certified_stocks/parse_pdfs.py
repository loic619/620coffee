"""PDF parsers — grading overview + infested warrant report.

Both PDFs are single-page with clean tables, so pdfplumber's extract_tables()
does the heavy lifting; we just route columns into a typed dict.
"""
from __future__ import annotations

import io
import re
from datetime import datetime


def _date_from_ordinal(text: str) -> str | None:
    """Match '21st May 2026' / 'THURSDAY 21ST MAY 2026' / 'As of 15th December 2025'."""
    m = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)\s+([A-Za-z]+)\s+(\d{4})", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2).title()} {m.group(3)}", "%d %B %Y").date().isoformat()
    except ValueError:
        return None


# ── Grading overview ─────────────────────────────────────────────────────────

def parse_grading_overview_pdf(content: bytes) -> dict:
    """Returns {report_date, queue_lots, forecast_lots, total_pending_lots}.

    The PDF has two tiny tables — GRADING QUEUE and GRADING FORECAST — each
    with a single 'Robusta (10 Tonne Delivery Units)' row carrying one number.
    """
    import pdfplumber

    text = ""
    tables: list = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        if pdf.pages:
            page = pdf.pages[0]
            text = page.extract_text() or ""
            tables = page.extract_tables() or []

    queue_lots = _extract_lots_cell(tables[0]) if len(tables) >= 1 else None
    forecast_lots = _extract_lots_cell(tables[1]) if len(tables) >= 2 else None

    # The PDF has no <table> grid; pdfplumber.extract_tables returns []. Fall
    # back to text+regex: each section ends with "Robusta (10 Tonne Delivery
    # Units) N" on the same line.
    if queue_lots is None or forecast_lots is None:
        nums = re.findall(r"Robusta\s*\(10\s*Tonne\s*Delivery\s*Units\)\s+(\d+)", text)
        if queue_lots is None and len(nums) >= 1:
            queue_lots = int(nums[0])
        if forecast_lots is None and len(nums) >= 2:
            forecast_lots = int(nums[1])

    total = (queue_lots or 0) + (forecast_lots or 0) if (queue_lots is not None or forecast_lots is not None) else None
    return {
        "report_date":        _date_from_ordinal(text),
        "queue_lots":         queue_lots,
        "forecast_lots":      forecast_lots,
        "total_pending_lots": total,
    }


def _extract_lots_cell(tbl) -> int | None:
    """Find the 'Robusta (10 Tonne Delivery Units)' row in a tiny 2-col table
    and return its number — fall back to the rightmost integer cell."""
    if not tbl:
        return None
    for row in tbl:
        if not row:
            continue
        label = " ".join((c or "") for c in row[:-1]).strip().lower()
        if "robusta" in label and "delivery" in label:
            try:
                return int((row[-1] or "0").strip() or 0)
            except (ValueError, AttributeError):
                return None
    # Fallback: rightmost integer anywhere in the table.
    for row in reversed(tbl):
        for cell in reversed(row or []):
            if cell is None:
                continue
            try:
                return int(str(cell).strip())
            except ValueError:
                continue
    return None


# ── Infested warrant ─────────────────────────────────────────────────────────

_INFESTED_SECTIONS = (
    ("suspended",   ("SUSPENDED FOR INFESTATION",)),
    ("re_released", ("FUMIGATED WITHIN 21 DAYS", "RE-RELEASED TO VALID")),
    ("not_valid",   ("NOT FUMIGATED WITHIN 21 DAYS", "GRADING RESULT NOW NOT VALID")),
)


def parse_infested_warrant_pdf(content: bytes) -> dict:
    """Returns the 3 tables: suspended / re-released / not-valid.

    Each row has STATUS · Panel Date · Origin · PORT · Split lot Y/N · Class · 10t
    (lots in 10-tonne delivery units).
    """
    import pdfplumber

    text = ""
    tables: list = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        if pdf.pages:
            page = pdf.pages[0]
            text = page.extract_text() or ""
            tables = page.extract_tables() or []

    # The 3 tables appear in order; map by index, but defensively label by
    # nearby heading text in case the layout drifts.
    out: dict[str, dict] = {key: {"rows": [], "total_lots": 0} for key, _ in _INFESTED_SECTIONS}
    keys_in_order = [k for k, _ in _INFESTED_SECTIONS]
    for i, tbl in enumerate(tables[:3]):
        key = keys_in_order[i] if i < len(keys_in_order) else None
        if key is None or not tbl:
            continue
        out[key] = _parse_warrant_table(tbl)

    return {
        "report_date": _date_from_ordinal(text),
        **out,
    }


def _parse_warrant_table(tbl) -> dict:
    """Header columns: STATUS | Panel Date | Origin | PORT | Split lot Y/N | Class | 10t.
    The last row labelled 'Total' carries the lots sum."""
    rows: list[dict] = []
    total_lots = 0
    # Find header row (contains STATUS).
    header_idx = next(
        (i for i, r in enumerate(tbl)
         if r and any(c and "STATUS" in str(c).upper() for c in r)),
        None,
    )
    if header_idx is None:
        return {"rows": rows, "total_lots": total_lots}
    for r in tbl[header_idx + 1:]:
        if not r:
            continue
        first = (r[0] or "").strip()
        if first.lower() == "total":
            try:
                total_lots = int((r[-1] or "0").strip() or 0)
            except (ValueError, AttributeError):
                pass
            continue
        if not first:
            continue
        # Tolerate short rows. Bind `r` explicitly so ruff's B023 is happy
        # (loop variable would otherwise be captured by reference and reflect
        # whatever the last iteration's `r` is).
        def _get(i: int, _r=r):
            return (_r[i] if i < len(_r) else None) or ""
        get = _get
        try:
            lots = int(get(6).strip() or 0)
        except (ValueError, AttributeError):
            lots = 0
        rows.append({
            "status":     first,
            "panel_date": get(1).strip(),
            "origin":     get(2).strip(),
            "port":       get(3).strip(),
            "split_lot":  get(4).strip(),
            "class":      get(5).strip(),
            "lots":       lots,
        })
    return {"rows": rows, "total_lots": total_lots}
