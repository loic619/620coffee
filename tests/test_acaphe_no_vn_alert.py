"""A tick with no VN bids must not be silent when vietnam_last has gone stale.

Register #262. The poller wrote `vietnam_last` only when acaphe served VN bids;
otherwise it printed one line and `--once` exited 0. A whole VN morning with no
VN prices therefore passed with every run green, on 2026-09-29 and again on
2026-10-23, and was found a day later by 619's check 2.04 (26 h limit).

Positive control: against the code before this change,
`test_no_vn_bids_and_30h_old_snapshot_fails_the_tick` exits 0 and prints no
`::error::` — it fails on both asserts.
"""
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fetch"))

from scraper import acaphe_poller as ap

# Thursday 2026-10-29, 05:00 UTC = 12:00 ICT: inside the VN window.
IN_WINDOW = datetime(2026, 10, 29, 5, 0, tzinfo=UTC)
# Same Thursday, 13:00 UTC: trading hours, VN prices stripped by design.
AFTERNOON = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)

VN_HIGH = ("09:46 29/10 (x) BMT bid 86000-86500 / offer 87000-87500 "
           "HCM bid 88000-88500 / offer 89000-89500")


def _rows(vn_high: str) -> list[dict]:
    return [
        {"stt": "0", "Month": "RF26", "Last": "4500", "Change": "10"},
        {"stt": "1", "Month": "AH26", "Last": "380.5", "Change": "-1"},
        {"stt": "14", "High": vn_high, "Low": "26,070 (11:26 Oct 29)"},
    ]


class _Resp:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows
        self.text = json.dumps(rows)

    def raise_for_status(self):
        pass

    def json(self):
        return self._rows


@pytest.fixture
def poller(tmp_path, monkeypatch):
    """Patch network, clock and output paths; return the Redis writes made."""
    pushes: list[tuple[str, dict]] = []
    state = {"rows": _rows(""), "last_vn": None, "now": IN_WINDOW}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"] if tz else state["now"].replace(tzinfo=None)

    monkeypatch.setattr(ap, "datetime", _Clock)
    monkeypatch.setattr(ap, "OUTPUT", tmp_path / "acaphe_live.json")
    monkeypatch.setattr(ap, "VIETNAM_LAST", tmp_path / "vietnam_last.json")
    monkeypatch.setattr(ap, "DATABASE_URL", "")
    monkeypatch.setattr(ap, "VN_ONLY", False)
    monkeypatch.setattr(ap.requests, "get", lambda *a, **k: _Resp(state["rows"]))
    monkeypatch.setattr(ap, "_get_redis", lambda key: state["last_vn"] if key == "vietnam_last" else None)
    monkeypatch.setattr(ap, "_push_redis", lambda data, key=ap.REDIS_KEY, ttl_s=None: pushes.append((key, data)))

    async def _cookies(force_login=False):
        return {}, False

    monkeypatch.setattr(ap, "get_cookies", _cookies)
    monkeypatch.setattr(sys, "argv", ["acaphe_poller", "--once"])
    state["pushes"] = pushes
    state["tmp"] = tmp_path
    return state


def _run_once() -> int:
    with pytest.raises(SystemExit) as exc:
        asyncio.run(ap.main())
    return exc.value.code


def _snapshot(saved_at: str) -> dict:
    return {"bmt_bid": "85000-85500", "hcm_bid": "87000-87500", "saved_at": saved_at}


def test_no_vn_bids_and_30h_old_snapshot_fails_the_tick(poller, capsys):
    # 30 weekday hours before Thursday 05:00 UTC.
    poller["last_vn"] = _snapshot("2026-10-27T23:00:00+00:00")
    assert _run_once() == 1
    out = capsys.readouterr().out
    assert "::error::vietnam_last is 30.0 weekday hours old" in out
    assert not any(key == "vietnam_last" for key, _ in poller["pushes"])


def test_no_vn_bids_and_2h_old_snapshot_warns_and_exits_0(poller, capsys):
    poller["last_vn"] = _snapshot("2026-10-29T03:00:00Z")
    assert _run_once() == 0
    out = capsys.readouterr().out
    assert "::warning::acaphe served no VN bids in the VN window" in out
    assert "::error::" not in out
    # The last-known snapshot is still injected into live_quotes, as before.
    live = [d for key, d in poller["pushes"] if key == ap.REDIS_KEY]
    assert live and live[0]["vietnam"]["saved_at"] == "2026-10-29T03:00:00Z"


def test_vn_bids_present_saves_as_before(poller, capsys):
    poller["rows"] = _rows(VN_HIGH)
    poller["last_vn"] = _snapshot("2026-10-20T00:00:00Z")  # stale, but irrelevant
    assert _run_once() == 0
    out = capsys.readouterr().out
    assert "Vietnam snapshot saved" in out
    assert "::error::" not in out and "::warning::" not in out
    saved = [d for key, d in poller["pushes"] if key == "vietnam_last"]
    assert len(saved) == 1
    assert saved[0]["bmt_bid"] == "86000-86500"
    assert saved[0]["saved_at"] == IN_WINDOW.isoformat()
    on_disk = json.loads((poller["tmp"] / "vietnam_last.json").read_text())
    assert on_disk == saved[0]


def test_fresh_snapshot_outside_the_vn_window_is_quiet(poller, capsys):
    """Afternoon ticks lose VN prices by design: no annotation, exit 0."""
    poller["now"] = AFTERNOON
    poller["last_vn"] = _snapshot("2026-10-29T08:31:00Z")
    assert _run_once() == 0
    out = capsys.readouterr().out
    assert "::warning::" not in out and "::error::" not in out


def test_stale_snapshot_fails_even_outside_the_vn_window(poller, capsys):
    """26 weekday hours always spans a whole VN window, so the staleness
    itself is the evidence; gating it on the morning clock would delay the
    alarm by a day (the day's last save lands at ~08:31 UTC)."""
    poller["now"] = AFTERNOON
    poller["last_vn"] = _snapshot("2026-10-28T08:31:00Z")  # 28.5 h
    assert _run_once() == 1
    assert "::error::" in capsys.readouterr().out


def test_monday_morning_does_not_count_the_weekend(poller, capsys):
    """Friday 08:31 -> Monday 00:07 UTC is 63.6 h of wall clock, 15.6 weekday h."""
    poller["now"] = datetime(2026, 11, 2, 0, 7, tzinfo=UTC)  # Monday
    poller["last_vn"] = _snapshot("2026-10-30T08:31:00Z")    # Friday
    assert _run_once() == 0
    out = capsys.readouterr().out
    assert "::error::" not in out
    assert "::warning::" in out and "15.6 weekday hours" in out


def test_unreadable_snapshot_warns_in_window(poller, capsys):
    poller["last_vn"] = None
    assert _run_once() == 0
    assert "::warning::acaphe served no VN bids and vietnam_last is unreadable" in capsys.readouterr().out


# ── Once per UTC weekday ──────────────────────────────────────────────────────
# 2026-11-25: vietnam_last went past 26 h at 10:31 UTC and every 15-minute tick
# after it failed, one failure email each, for the same stale snapshot.

STALE_SAVED = "2026-11-24T08:31:25Z"                     # the real 2026-11-25 case
WED_1045 = datetime(2026, 11, 25, 10, 45, tzinfo=UTC)    # first stale tick, 26.2 h


@pytest.fixture
def store(poller, monkeypatch):
    """A dict standing in for Upstash, so the alert marker persists across ticks."""
    kv: dict = {}
    monkeypatch.setattr(ap, "_get_redis",
                        lambda key: poller["last_vn"] if key == "vietnam_last" else kv.get(key))

    def _push(data, key=ap.REDIS_KEY, ttl_s=None):
        poller["pushes"].append((key, data))
        kv[key] = data

    monkeypatch.setattr(ap, "_push_redis", _push)
    poller["last_vn"] = _snapshot(STALE_SAVED)
    poller["now"] = WED_1045
    return kv


def test_first_stale_tick_of_the_day_fails_and_records_the_date(store, poller, capsys):
    """Positive control: the once-per-day rule must not swallow the first alert."""
    assert _run_once() == 1
    assert "::error::vietnam_last is 26.2 weekday hours old" in capsys.readouterr().out
    assert store[ap.VN_ALERT_KEY] == {"date": "2026-11-25", "saved_at": STALE_SAVED}


def test_later_stale_ticks_the_same_day_warn_and_exit_0(store, poller, capsys):
    assert _run_once() == 1
    capsys.readouterr()
    for h, m in [(11, 0), (11, 15), (18, 45), (23, 59)]:
        poller["now"] = datetime(2026, 11, 25, h, m, tzinfo=UTC)
        assert _run_once() == 0
        out = capsys.readouterr().out
        assert "::error::" not in out
        assert "::warning::vietnam_last is" in out and "already failed a tick on 2026-11-25" in out
    # The marker is written once, by the tick that failed — not refreshed by the warnings.
    assert sum(1 for key, _ in poller["pushes"] if key == ap.VN_ALERT_KEY) == 1


def test_still_stale_next_weekday_fails_again(store, poller, capsys):
    assert _run_once() == 1
    poller["now"] = datetime(2026, 11, 26, 0, 7, tzinfo=UTC)  # Thursday, first tick
    assert _run_once() == 1
    assert "::error::" in capsys.readouterr().out
    assert store[ap.VN_ALERT_KEY]["date"] == "2026-11-26"


def test_stale_on_friday_is_not_re_alerted_on_the_weekend_but_is_on_monday(store, poller, capsys):
    poller["last_vn"] = _snapshot("2026-11-26T08:31:00Z")      # Thursday
    poller["now"] = datetime(2026, 11, 27, 11, 0, tzinfo=UTC)  # Friday, 26.5 h
    assert _run_once() == 1
    poller["now"] = datetime(2026, 11, 28, 3, 30, tzinfo=UTC)  # Saturday dispatch
    assert _run_once() == 0
    assert "weekend tick" in capsys.readouterr().out
    poller["now"] = datetime(2026, 11, 30, 0, 7, tzinfo=UTC)   # Monday
    assert _run_once() == 1


def test_marker_unreadable_fails_toward_alerting(store, poller, monkeypatch, capsys):
    """Upstash down for the marker: no date to compare, so the tick fails as before."""
    monkeypatch.setattr(ap, "_get_redis",
                        lambda key: poller["last_vn"] if key == "vietnam_last" else None)
    assert _run_once() == 1
    poller["now"] = datetime(2026, 11, 25, 11, 0, tzinfo=UTC)
    assert _run_once() == 1


def test_fresh_snapshot_ignores_a_marker_from_today(store, poller, capsys):
    """The marker only downgrades a stale snapshot; it never hides anything else."""
    store[ap.VN_ALERT_KEY] = {"date": "2026-10-29", "saved_at": "x"}
    poller["now"] = IN_WINDOW
    poller["last_vn"] = _snapshot("2026-10-29T03:00:00Z")
    assert _run_once() == 0
    assert "::warning::acaphe served no VN bids in the VN window" in capsys.readouterr().out


def test_weekday_hours_between():
    fri = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    mon = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert ap._weekday_hours_between(fri, mon) == 24.0
    assert ap._weekday_hours_between(mon, fri) == 0.0
