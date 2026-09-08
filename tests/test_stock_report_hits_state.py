"""The dated publish-time log: what it stores, and how tiers 0 and 1 read it.

`stock_report_hits.json` is the learned state behind the whole tiered search.
One entry per date, `{"date": "YYYY-MM-DD", "hhmmss": "HHMMSS"}`:

    Tier 0   the second recorded for THIS date  -> one GET
    Tier 1   the top-K most frequent seconds across the WHOLE log, +/-2s

Both repos run this code and both learn from this file, so its semantics are a
parity surface, not an implementation detail. The twin of this file lives at
`backend/scraper/tests/test_stock_report_hits_state.py` in 619coffee and
everything below the PARITY BODY marker is identical in both;
`backend/scripts/check_620_fetch_parity.py` there fails CI if they drift. If you
change a semantic here, change it there.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fetch"))

from scraper.sources.ice_certified_stocks import orchestrate as o  # noqa: E402

# ── PARITY BODY: identical in 619 and 620 below this line ────────────────────


@pytest.fixture
def hits(tmp_path, monkeypatch):
    """Point the hits log at a scratch file. Returns the path so a test can
    inspect exactly what was written to disk, not just what the loader says."""
    path = tmp_path / "stock_report_hits.json"
    monkeypatch.setattr(o, "STOCK_REPORT_HITS_PATH", path)
    return path


# ── the shape on disk ────────────────────────────────────────────────────────

def test_a_capture_is_recorded_against_its_date(hits):
    o._record_stock_report_hit(date(2026, 9, 7), "104834")

    assert json.loads(hits.read_text()) == {
        "hits": [{"date": "2026-09-07", "hhmmss": "104834"}]
    }


def test_the_log_is_a_dated_list_not_a_counts_aggregate(hits):
    """Pins the state model itself. Both repos learn tier 1 from the SAME
    dated history — one entry per date — so tier 0 works in both and the two
    tier-1 lists are derived from identical input. An undated counts map would
    serve tier 1 and silently lose tier 0, which is what this forbids."""
    for day, second in ((3, "103033"), (4, "103033"), (5, "104500")):
        o._record_stock_report_hit(date(2026, 9, day), second)

    written = json.loads(hits.read_text())
    assert set(written) == {"hits"}
    assert isinstance(written["hits"], list)
    assert all(set(h) == {"date", "hhmmss"} for h in written["hits"])


# ── update semantics ─────────────────────────────────────────────────────────

def test_one_entry_per_date_and_the_latest_wins(hits):
    o._record_stock_report_hit(date(2026, 9, 7), "103033")
    o._record_stock_report_hit(date(2026, 9, 7), "104500")      # corrected

    entries = json.loads(hits.read_text())["hits"]
    assert len(entries) == 1
    assert o._recorded_time_for(date(2026, 9, 7)) == "104500"


def test_recording_one_date_leaves_the_others_alone(hits):
    o._record_stock_report_hit(date(2026, 9, 3), "103033")
    o._record_stock_report_hit(date(2026, 9, 4), "104500")
    o._record_stock_report_hit(date(2026, 9, 3), "105119")

    assert o._recorded_time_for(date(2026, 9, 4)) == "104500"
    assert o._recorded_time_for(date(2026, 9, 3)) == "105119"


def test_the_log_is_capped_and_drops_the_oldest_first(hits):
    for i in range(405):
        o._record_stock_report_hit(date(2025, 1, 1) + timedelta(days=i), "103033")

    entries = json.loads(hits.read_text())["hits"]
    assert len(entries) == 400
    assert entries[0]["date"] == (date(2025, 1, 1) + timedelta(days=5)).isoformat()
    assert entries[-1]["date"] == (date(2025, 1, 1) + timedelta(days=404)).isoformat()


# ── tier 0 ───────────────────────────────────────────────────────────────────

def test_tier0_is_an_exact_date_lookup_and_never_guesses(hits):
    o._record_stock_report_hit(date(2026, 9, 7), "104834")

    assert o._recorded_time_for(date(2026, 9, 7)) == "104834"
    assert o._recorded_time_for(date(2026, 9, 8)) is None        # next day
    assert o._recorded_time_for(date(2026, 9, 6)) is None        # previous day


def test_tier0_is_unavailable_before_anything_is_recorded(hits):
    assert o._load_stock_report_hits() == []
    assert o._recorded_time_for(date(2026, 9, 7)) is None


# ── tier 1 ───────────────────────────────────────────────────────────────────

def _write(path, pairs):
    path.write_text(json.dumps(
        {"hits": [{"date": d, "hhmmss": t} for d, t in pairs]}, indent=2))


def test_tier1_ranks_by_frequency_across_the_whole_log(hits):
    _write(hits, [("2026-09-01", "103100"),
                  ("2026-09-02", "105900"), ("2026-09-03", "105900"),
                  ("2026-09-04", "105900")])

    assert o._stock_report_tier1_times()[0] == "105900"


def test_tier1_ranking_carries_no_added_tie_break(hits):
    """619's ranking ported unchanged: `sorted(..., key=lambda kv: -kv[1])`.
    Python's sort is stable, so equally-frequent seconds keep first-appearance
    order in the log. An ascending-second tie-break was tried and measured on
    the real history: it clusters the +/-2s windows and covered 21% of days
    against 28%. Anything that reorders ties changes what both repos ask for."""
    _write(hits, [("2026-09-01", "105900"),
                  ("2026-09-02", "103100"),
                  ("2026-09-03", "104500")])

    bases = [t for t in o._stock_report_tier1_times() if t in
             {"105900", "103100", "104500"}]
    assert bases == ["105900", "103100", "104500"], (
        "tier-1 ties are no longer in first-appearance order — a tie-break has "
        "been added, and 619 and 620 would search in different orders"
    )


def test_tier1_takes_at_most_K_seconds_each_widened_by_2s(hits):
    _write(hits, [(f"2026-01-{i + 1:02d}", f"1030{i:02d}") for i in range(30)])

    times = o._stock_report_tier1_times()
    assert len(set(times)) == len(times), "tier 1 offered the same second twice"
    assert len(times) <= o.STOCK_REPORT_TIER1_K * 5, "more than K bases, +/-2s"
    assert all(len(t) == 6 and t.isdigit() for t in times)


def test_tier1_asks_the_exact_second_before_its_neighbours(hits):
    _write(hits, [("2026-09-01", "103300")])

    times = list(o._stock_report_tier1_times())
    assert times[:5] == ["103300", "103259", "103301", "103258", "103302"]


def test_tier1_falls_back_to_bootstrap_guesses_on_an_empty_log(hits):
    times = o._stock_report_tier1_times()

    assert times, "tier 1 produced no candidates at all"
    assert all(len(t) == 6 and t.isdigit() for t in times)


def test_a_corrupt_log_does_not_kill_the_fetch(hits):
    hits.write_text("{ not json")

    assert o._load_stock_report_hits() == []
    assert o._recorded_time_for(date(2026, 9, 7)) is None
    assert o._stock_report_tier1_times()          # bootstrap guesses


def test_entries_without_a_second_are_ignored(hits):
    _write(hits, [("2026-09-01", "103300")])
    entries = json.loads(hits.read_text())["hits"]
    entries.append({"date": "2026-09-02"})
    hits.write_text(json.dumps({"hits": entries}))

    assert o._recorded_time_for(date(2026, 9, 2)) is None
    assert "103300" in o._stock_report_tier1_times()
