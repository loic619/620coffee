"""Pin the pacing experiment so it cannot drift or be changed by accident.

620's ICE fetch is refused by ICE on the public runner. Two clean observations,
both at 619's 3.0s sweep interval:

    run 34198991714   24 x 403, 261 x 404, 0 snapshots  (also died on a lazy import)
    run 34202188869   35 x 403,   0 x 404, 0 snapshots, 4 sections blocked, 2m25s
                      403 on the FIRST request of every section

619, same day, private runner, same code:
    04:13   1,191 requests   12 x 200   0 x 403
    05:08      17 requests   12 x 200   0 x 403
    06:39   success

The experiment raises the sweep interval to 5.0s and changes NOTHING else, so
that a difference in outcome has exactly one candidate cause. These tests exist
so the isolation is enforced rather than merely intended: if someone adjusts a
second knob while this is running, the experiment is void and CI says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetch"))

from scraper.sources.ice_certified_stocks import orchestrate  # noqa: E402

EXPERIMENT_INTERVAL_S = 5.0
UPSTREAM_619_INTERVAL_S = 3.0


def test_the_sweep_interval_is_the_experimental_value():
    assert orchestrate._STOCK_SWEEP_INTERVAL_S == EXPERIMENT_INTERVAL_S, (
        f"the pacing experiment requires a {EXPERIMENT_INTERVAL_S}s sweep interval; "
        f"found {orchestrate._STOCK_SWEEP_INTERVAL_S}s"
    )
    assert orchestrate._STOCK_SWEEP_INTERVAL_S != UPSTREAM_619_INTERVAL_S, (
        "the interval matches 619's, so nothing is being tested"
    )


def test_no_second_variable_was_changed():
    """Single-variable means single. These are 619's values and must stay so."""
    assert orchestrate._THROTTLE == {"public": 2.0, "marketdata": 5.0}, (
        f"_THROTTLE is {orchestrate._THROTTLE}, not 619's values — a second "
        f"variable has changed and the experiment no longer isolates cadence"
    )
    assert orchestrate._THROTTLE_CAP == 15.0
    assert orchestrate.TOO_MANY_403S == 8, "the 403 bail-out is part of the baseline"
    assert orchestrate.TOO_MANY_429S == 4
    assert orchestrate.TIMEOUT == 30


def test_retry_behaviour_is_untouched():
    """Named explicitly because retries are the thing one reaches for next."""
    assert orchestrate.RETRY_AFTER_MAX_S == 90
    assert orchestrate.RETRY_AFTER_GIVE_UP_S == 600


def test_the_port_still_differs_from_619_in_exactly_three_places():
    """The real 'nothing else changed' invariant.

    orchestrate.py is a byte-identical copy of 619's apart from hunks marked
    PORTED TO 620: two path anchors, plus this experiment. A fourth marker means
    a second variable was introduced and the experiment no longer isolates
    cadence. Asserting on the markers beats asserting on request-construction
    strings, which are part of the untouched baseline and whose absence would
    prove nothing.
    """
    source = (ROOT / "fetch" / "scraper" / "sources" / "ice_certified_stocks"
              / "orchestrate.py").read_text()
    markers = source.count("PORTED TO 620") + source.count("PACING EXPERIMENT")
    assert markers == 3, (
        f"expected 3 documented divergences from 619 (2 path anchors + the "
        f"pacing experiment), found {markers}"
    )
