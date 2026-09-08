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
MARKETDATA_S = 5.0            # /marketdata/ — LIFFE

# What 619 uses. Kept here so the divergence is legible.
UPSTREAM_619 = {"sweep": 3.0, "public": 2.0, "marketdata": 5.0}


def test_the_pacing_baseline_is_the_validated_one():
    assert orchestrate._STOCK_SWEEP_INTERVAL_S == SWEEP_INTERVAL_S
    assert orchestrate._THROTTLE["public"] == PUBLICDOCS_S
    assert orchestrate._THROTTLE["marketdata"] == MARKETDATA_S


def test_the_public_runner_is_paced_slower_than_619():
    """The whole point. 619's values are refused on this runner pool."""
    assert orchestrate._STOCK_SWEEP_INTERVAL_S > UPSTREAM_619["sweep"]
    assert orchestrate._THROTTLE["public"] > UPSTREAM_619["public"]


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
