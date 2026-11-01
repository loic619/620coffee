"""Arabica grading: absent is not zero (619 commit 56de4db77, ACTIONS_ISSUES A47).

A grading section whose wording the parser does not recognise, or a report with
no grading section at all, used to come back as passed = failed = 0 — in the
published window indistinguishable from ICE's own "No Bags Passed Today". It
must now come back as None (null in JSON), with a warning. ICE's real zero stays
0 and silent.

Adapted from 619's backend/scraper/tests/test_arabica_grading.py; only the
sys.path anchor differs, since the package lives under fetch/ here.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetch"))

from scraper.sources.ice_certified_stocks.parse_arabica_xls import (  # noqa: E402
    _parse_grading_summary,
)


class _FakeSheet:
    """Minimal xlrd-sheet stand-in: a list of rows (lists of cell values)."""

    def __init__(self, rows: list[list]):
        self.rows = rows
        self.nrows = len(rows)
        self.ncols = max((len(r) for r in rows), default=0)

    def cell_value(self, r: int, c: int):
        row = self.rows[r]
        return row[c] if c < len(row) else ""


_HEAD = [["ICE Futures U.S."]] * 55  # grading summary starts at row 55


def test_grading_unrecognised_wording_is_null_not_zero(caplog):
    rows = _HEAD + [
        ["TODAY'S GRADING SUMMARY"],
        ["Grading results: none recorded for this session"],   # unknown wording
        ["Pending Grading Report"],
    ]
    with caplog.at_level("WARNING"):
        out = _parse_grading_summary(_FakeSheet(rows), 55)
    assert out["passed_today_bags"] is None
    assert out["failed_today_bags"] is None
    assert out["passed_text"] is None and out["failed_text"] is None
    assert any("stored as null" in r.getMessage() for r in caplog.records)


def test_grading_no_bags_today_is_real_zero_and_silent(caplog):
    """Control: ICE's own 'No Bags …' lines are a genuine 0, with no warning."""
    rows = _HEAD + [
        ["TODAY'S GRADING SUMMARY"],
        ["No Bags Passed Today"],
        ["No Bags Failed Today"],
        ["Pending Grading Report"],
    ]
    with caplog.at_level("WARNING"):
        out = _parse_grading_summary(_FakeSheet(rows), 55)
    assert out["passed_today_bags"] == 0 and out["failed_today_bags"] == 0
    assert out["passed_text"] == "No Bags Passed Today"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_snapshot_keeps_null_and_source_text():
    from scraper.sources.ice_certified_stocks.orchestrate import _arabica_snapshot

    # Report with no grading section at all -> None, never 0.
    snap = _arabica_snapshot(date(2026, 9, 23), {"report_date": "2026-09-23", "grading_today": None})
    assert snap["passed_today_bags"] is None and snap["failed_today_bags"] is None
    assert snap["passed_text"] is None

    # Real ICE zero -> 0, and the source line travels with the row.
    gt = {"passed_today_bags": 0, "failed_today_bags": 0,
          "passed_text": "No Bags Passed Today", "failed_text": "No Bags Failed Today",
          "passed_detail": None, "failed_detail": None}
    snap = _arabica_snapshot(date(2026, 9, 28), {"report_date": "2026-09-28", "grading_today": gt})
    assert snap["passed_today_bags"] == 0 and snap["failed_today_bags"] == 0
    assert snap["passed_text"] == "No Bags Passed Today"
    assert snap["failed_text"] == "No Bags Failed Today"
