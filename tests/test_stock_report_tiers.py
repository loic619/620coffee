"""Tier ordering and duplicate-freedom in the robusta stock-report search.

The rule:

    Tier 0   the second already recorded for THIS date — one GET, first
    Tier 1   the top-K seconds from the whole hit log, ±2s, minus tier 0 —
             a real fast path, ahead of the sweep
    Tier 2   the window ascending, minus everything tiers 0 and 1 asked

Each second is requested at most once per date. Tier 1 used to be deferred into
the sweep on sweep days (#839), which is correct about coverage and wrong about
latency: the sweep walks ascending from 10:29:50, so a 10:39:12 publish is the
~1,400th request, while the same second sits in the top-10 list.

Every test below asserts that requests were actually issued. Without that, a
_RATE_STATE left dirty by an earlier test makes _http_get return None for
everything and each ordering assertion passes vacuously over an empty list.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fetch"))

from scraper.sources.ice_certified_stocks import orchestrate as o  # noqa: E402

# ── PARITY BODY: identical in 619 and 620 below this line ────────────────────

DAY = date(2026, 9, 7)


@pytest.fixture(autouse=True)
def _clean_rate_state():
    """A blocked run issues no requests, which would make these tests vacuous."""
    o._RATE_STATE.update(consecutive_429s=0, consecutive_403s=0,
                         aborted=0, section_blocked=0)
    yield
    o._RATE_STATE.update(consecutive_429s=0, consecutive_403s=0,
                         aborted=0, section_blocked=0)


@pytest.fixture
def calls(monkeypatch):
    """Record every second requested, in order. Every request answers 'miss'."""
    seen: list[str] = []

    def fake_get(url, *, source=None, _retry=False):
        seen.append(url.rsplit("_", 1)[-1].split(".")[0])
        return None

    monkeypatch.setattr(o, "_http_get", fake_get)
    monkeypatch.setattr(o, "_save_cursor", lambda *a, **k: None)
    monkeypatch.setattr(o, "_mark_tier1_tried", lambda *a, **k: None)
    monkeypatch.setattr(o, "_tier1_already_tried", lambda *a, **k: False)
    monkeypatch.setattr(o, "_load_cursor", lambda: {})
    return seen


@pytest.fixture
def hints(monkeypatch):
    """A deterministic tier-1 list: three inside the window, one outside."""
    times = ("103300", "104500", "105900", "112351")
    monkeypatch.setattr(o, "_stock_report_tier1_times", lambda: times)
    return times


def _tier0(monkeypatch, hhmmss: str | None):
    monkeypatch.setattr(o, "_recorded_time_for", lambda d: hhmmss)


# ── ordering ─────────────────────────────────────────────────────────────────

def test_tier0_is_attempted_first(calls, hints, monkeypatch):
    _tier0(monkeypatch, "101010")
    o.pull_stock_report(DAY, sweep=True)

    assert calls, "no requests were issued — the assertions below would be vacuous"
    assert calls[0] == "101010"


def test_tier1_follows_tier0_and_precedes_the_sweep(calls, hints, monkeypatch):
    _tier0(monkeypatch, "101010")
    o.pull_stock_report(DAY, sweep=True)

    assert calls
    assert calls[0] == "101010"
    assert calls[1:5] == list(hints), "tier 1 did not run immediately after tier 0"

    first_sweep = o._stock_report_sweep_times()[0]
    assert calls.index(first_sweep) > 4, "the sweep started before tier 1 finished"


def test_tier1_is_a_fast_path_not_deferred_into_the_sweep(calls, hints, monkeypatch):
    """The regression this change fixes: in-window tier-1 times must be tried
    up front, not left for the sweep to reach ~1,400 requests later."""
    _tier0(monkeypatch, None)
    o.pull_stock_report(DAY, sweep=True)

    assert calls
    in_window = set(o._stock_report_sweep_times())
    tried_early = [t for t in calls[:len(hints)] if t in in_window]
    assert tried_early, "every in-window tier-1 time was deferred to the sweep"
    assert calls[:len(hints)] == list(hints)


# ── exclusion ────────────────────────────────────────────────────────────────

def test_tier0_is_excluded_from_tier1(calls, monkeypatch):
    """When the recorded second is also a popular one, it must not be re-asked."""
    monkeypatch.setattr(o, "_stock_report_tier1_times",
                        lambda: ("103300", "104500"))
    _tier0(monkeypatch, "103300")
    o.pull_stock_report(DAY, sweep=False)

    assert calls
    assert calls.count("103300") == 1, "tier 1 re-asked tier 0's second"
    assert calls == ["103300", "104500"]


def test_tier0_and_tier1_are_excluded_from_the_sweep(calls, hints, monkeypatch):
    _tier0(monkeypatch, "103300")          # deliberately also a tier-1 second
    o.pull_stock_report(DAY, sweep=True)

    assert calls
    sweep_portion = calls[len(set(hints)):]
    for asked in set(hints) | {"103300"}:
        assert asked not in sweep_portion, f"the sweep re-asked {asked}"


# ── duplicate freedom ────────────────────────────────────────────────────────

def test_no_second_is_requested_twice(calls, hints, monkeypatch):
    _tier0(monkeypatch, "103300")
    o.pull_stock_report(DAY, sweep=True)

    assert calls
    duplicates = {t for t in calls if calls.count(t) > 1}
    assert not duplicates, f"requested more than once: {sorted(duplicates)}"


def test_no_duplicates_when_sweep_is_disabled(calls, hints, monkeypatch):
    _tier0(monkeypatch, "104500")
    o.pull_stock_report(DAY, sweep=False)

    assert calls
    assert len(calls) == len(set(calls))
    assert len(calls) <= 1 + len(hints)


# ── coverage is otherwise unchanged ──────────────────────────────────────────

def test_full_sweep_coverage_is_unchanged(calls, hints, monkeypatch):
    """Everything in the window is still asked exactly once, by one tier or
    another — skipping in tier 2 must not lose candidates."""
    _tier0(monkeypatch, "103300")
    o.pull_stock_report(DAY, sweep=True)

    assert calls
    window = set(o._stock_report_sweep_times())
    assert window <= set(calls), (
        f"{len(window - set(calls))} window second(s) were never requested"
    )


def test_the_sweep_still_walks_ascending(calls, hints, monkeypatch):
    _tier0(monkeypatch, None)
    o.pull_stock_report(DAY, sweep=True)

    assert calls
    sweep_portion = [t for t in calls[len(hints):]]
    assert sweep_portion == sorted(sweep_portion), "the sweep is no longer ascending"


# ── the guard against a vacuous pass ─────────────────────────────────────────

@pytest.mark.parametrize("flag", ["aborted", "section_blocked"])
def test_a_dirty_rate_state_short_circuits_every_request(flag):
    """Pins the failure mode the autouse fixture exists to prevent.

    _http_get returns None before doing anything when _RATE_STATE is dirty. If a
    previous test left it that way, pull_stock_report would issue nothing and
    every ordering assertion above would pass vacuously over an empty list —
    which is why each of them asserts `calls` is non-empty first.

    This tests the real _http_get, unpatched, so it must not be given the
    `calls` fixture: that fixture replaces the very function under test.
    """
    o._RATE_STATE[flag] = 1
    try:
        assert o._http_get("https://example.invalid/never-requested.csv") is None
    finally:
        o._RATE_STATE[flag] = 0
