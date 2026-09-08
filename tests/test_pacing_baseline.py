"""The 620 pacing baseline, pinned so it is an explicit decision not an accident.

ICE refuses GitHub's public-runner pool at 619's pacing. Two clean observations
at {sweep 3.0, public 2.0, marketdata 5.0}:

    run 34198991714   24 x 403, 261 x 404, 0 snapshots
    run 34202188869   35 x 403,   0 x 404, 0 snapshots, 4 sections blocked,
                      403 on the FIRST request of every section, 2m25s

619 the same day, private pool, same code: 1,191 requests, 12 x 200, 0 x 403.

Slower pacing cleared the refusal: the run progressed into the expected
404-heavy timestamp search rather than being refused from the first request.

These tests exist because the values are load-bearing and non-obvious. Someone
reading orchestrate.py against 619's copy will see three numbers that differ and
may "fix" them back. That would restore the 403.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetch"))

from scraper.sources.ice_certified_stocks import orchestrate  # noqa: E402

# The validated baseline, by endpoint family.
SWEEP_INTERVAL_S = 4.0        # tier-2 timestamp probing
PUBLICDOCS_S = 4.0            # /publicdocs/ — US reports
MARKETDATA_S = 8.0            # /marketdata/ — LIFFE

# The values that were REFUSED on the public runner pool. 619 has since adopted
# the same baseline as 620, so these are historical, not "what 619 uses".
REFUSED = {"sweep": 3.0, "public": 2.0, "marketdata": 5.0}

# The second-level sweep window, shared with 619.
SWEEP_WINDOW = ((10, 29, 50), (10, 59, 59))
SWEEP_CANDIDATES = 1810


def test_the_pacing_baseline_is_the_validated_one():
    assert orchestrate._STOCK_SWEEP_INTERVAL_S == SWEEP_INTERVAL_S
    assert orchestrate._THROTTLE["public"] == PUBLICDOCS_S
    assert orchestrate._THROTTLE["marketdata"] == MARKETDATA_S


def test_every_family_is_slower_than_the_refused_configuration():
    """Every family must exceed the values that were refused, marketdata
    included: 5.0 was its value in both refused runs, and 8.0 is the value
    observed clearing the block."""
    assert orchestrate._STOCK_SWEEP_INTERVAL_S > REFUSED["sweep"]
    assert orchestrate._THROTTLE["public"] > REFUSED["public"]
    assert orchestrate._THROTTLE["marketdata"] > REFUSED["marketdata"]


def test_the_sweep_window_matches_619():
    """Fetch parity. 619 moved to a second-level window on 2026-09-07; 620 was
    still walking whole minutes, giving 1,920 candidates over 10:29:00-11:00:59
    against 619's 1,810 over 10:29:50-10:59:59. Pinned so they cannot drift
    apart again unnoticed."""
    assert orchestrate.STOCK_REPORT_SWEEP_RANGE == SWEEP_WINDOW
    times = orchestrate._stock_report_sweep_times()
    assert len(times) == SWEEP_CANDIDATES, (
        f"{len(times)} sweep candidates, expected {SWEEP_CANDIDATES}"
    )
    assert times[0] == "102950"
    assert times[-1] == "105959"
    assert len(set(times)) == len(times), "duplicate candidates"


def test_a_full_sweep_fits_the_workflow_timeout():
    """1,810 x 4s = 121 minutes against a 150-minute timeout. If either the
    window or the pacing grows, this is the constraint that breaks first."""
    minutes = len(orchestrate._stock_report_sweep_times()) * orchestrate._STOCK_SWEEP_INTERVAL_S / 60
    assert minutes < 150, f"a full sweep would take {minutes:.0f} min, exceeding the timeout"


def test_tier1_degrades_to_bootstrap_without_hints():
    """620 has no committed hits file — the hints arrive from a secret at run
    time, or not at all. Either way tier 1 must produce candidates rather than
    failing, because the fetch has to work without the secret."""
    times = orchestrate._stock_report_tier1_times()
    assert times, "tier 1 produced no candidates"
    assert all(len(t) == 6 and t.isdigit() for t in times)


def test_marketdata_stays_slower_than_publicdocs():
    """Observed production behaviour, not a guess: /marketdata/ (LIFFE) needs a
    slower interval than /publicdocs/ (US reports)."""
    assert orchestrate._THROTTLE["marketdata"] > orchestrate._THROTTLE["public"], (
        "marketdata must remain the slower family"
    )


def test_the_three_intervals_are_not_collapsed_into_one():
    """A single global throttle would lose the per-family distinction that the
    403 investigation established. Guarded explicitly."""
    assert isinstance(orchestrate._THROTTLE, dict)
    assert set(orchestrate._THROTTLE) == {"public", "marketdata"}
    distinct = {orchestrate._STOCK_SWEEP_INTERVAL_S,
                orchestrate._THROTTLE["public"],
                orchestrate._THROTTLE["marketdata"]}
    assert len(distinct) >= 2, "the intervals have been collapsed to one value"


def test_the_403_and_429_bail_outs_are_unchanged():
    """Surviving a block well is separate from avoiding it, and still matters."""
    assert orchestrate.TOO_MANY_403S == 8
    assert orchestrate.TOO_MANY_429S == 4
    assert orchestrate._THROTTLE_CAP == 15.0
    assert orchestrate.TIMEOUT == 30
    assert orchestrate.RETRY_AFTER_MAX_S == 90
    assert orchestrate.RETRY_AFTER_GIVE_UP_S == 600
