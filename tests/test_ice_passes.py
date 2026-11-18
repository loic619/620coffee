"""The fetch runs as two budgeted passes, and arabica is published first.

Run 44052146050 (2026-11-17) is the case all of this answers: arabica's three
days were fetched in minutes, then one late robusta release walked the whole
1,810-candidate tier-2 sweep at 4 s each, 20 "holes" from June were re-fetched
along with their per-day sources only to be trimmed before publishing, the job
hit its 150-minute timeout, and the cancel published nothing at all.

orchestrate.py is byte-identical to 619's and is not edited; everything here
drives it through publish_ice.py. The integration tests run the real run()
with only `_http_get` faked, so a pull the stubs forgot shows up as a request.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetch"))
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("publish_ice", ROOT / "fetch" / "publish_ice.py")
publish_ice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish_ice)

from scraper.sources.ice_certified_stocks import orchestrate  # noqa: E402

FETCH = ROOT / ".github" / "workflows" / "fetch-ice-certified-stocks.yml"


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Resp:
    def __init__(self, status_code=404, text=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = {}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point every file orchestrate reads or writes at a tmpdir, and record
    every URL it asks for instead of sending it."""
    monkeypatch.setattr(orchestrate, "STOCK_REPORT_HITS_PATH", tmp_path / "hits.json")
    monkeypatch.setattr(orchestrate, "STOCK_REPORT_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(orchestrate, "RUN_STATS_PATH", tmp_path / "run_stats.json")
    monkeypatch.setattr(orchestrate, "OUT_DIR", tmp_path / "orig_stage")
    monkeypatch.setitem(orchestrate._RATE_STATE, "aborted", 0)
    monkeypatch.setitem(orchestrate._RATE_STATE, "section_blocked", 0)
    monkeypatch.setitem(orchestrate._RUN_STATS, "sweep_exhausted", False)
    monkeypatch.setitem(orchestrate._RUN_STATS, "sweep_gets", 0)
    monkeypatch.setattr(publish_ice, "STAGE", tmp_path / "_stage")
    monkeypatch.setattr(publish_ice, "OUT", tmp_path / "data" / "ice")
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"catalog_version": 1, "generated_at": "x", "datasets": []}')
    monkeypatch.setattr(publish_ice, "CATALOG", catalog)
    monkeypatch.setattr(publish_ice, "_record_telemetry", lambda *_a, **_k: None)

    clock = FakeClock()
    requested: list[str] = []

    def fake_get(url, *, source=None, _retry=False):
        requested.append(url)
        clock.t += orchestrate._throttle_for(url)
        return Resp(404)

    monkeypatch.setattr(orchestrate, "_http_get", fake_get)

    def write_hits(dates_times):
        (tmp_path / "hits.json").write_text(json.dumps(
            {"hits": [{"date": d, "hhmmss": t} for d, t in dates_times]}))

    return {"tmp": tmp_path, "clock": clock, "requested": requested, "write_hits": write_hits}


# ── the budget ───────────────────────────────────────────────────────────────

def test_the_budget_has_a_sweep_deadline_before_its_hard_deadline():
    clock = FakeClock()
    b = publish_ice.Budget(total_s=600, reserve_s=100, clock=clock)
    assert not b.sweep_over() and not b.hard_over()
    clock.t = 499
    assert not b.sweep_over()
    clock.t = 500
    assert b.sweep_over() and not b.hard_over()
    clock.t = 600
    assert b.hard_over()


def test_a_reserve_larger_than_the_budget_cannot_go_negative():
    b = publish_ice.Budget(total_s=60, reserve_s=10_000, clock=FakeClock())
    assert b.sweep_deadline == b.start


def test_the_reserve_grows_with_the_window():
    assert publish_ice.reserve_for(30) > publish_ice.reserve_for(3)


# ── the tier-2 sweep stops on the budget, cleanly ────────────────────────────

def _sweep(isolated, monkeypatch, total_s, reserve_s):
    budget = publish_ice.Budget(total_s, reserve_s, clock=isolated["clock"])
    monkeypatch.setattr(orchestrate, "_stock_report_sweep_times",
                        publish_ice._budgeted_sweep_times(orchestrate._stock_report_sweep_times, budget))
    pull = publish_ice._budgeted_stock_report(orchestrate.pull_stock_report, budget)
    return budget, pull(date(2026, 11, 16), sweep=True)


def test_positive_control_an_unbudgeted_sweep_walks_the_whole_window(isolated, monkeypatch):
    """Proves the harness reaches the real sweep: with a budget wider than the
    walk, all 1,810 candidates are asked and the window is declared exhausted —
    which is what run 44052146050 did, at 4 s each."""
    budget, result = _sweep(isolated, monkeypatch, total_s=10 * 3600, reserve_s=0)
    assert result == (None, None)
    assert len(isolated["requested"]) == orchestrate._sweep_candidate_count() == 1810
    assert orchestrate._RUN_STATS["sweep_exhausted"] is True
    assert budget.sweep_cut is None
    # Tier 1 at the 8 s marketdata throttle, then the sweep at 4 s: ~121 minutes.
    sweep_gets = orchestrate._RUN_STATS["sweep_gets"]
    assert isolated["clock"].t == pytest.approx(sweep_gets * 4.0 + (1810 - sweep_gets) * 8.0)
    assert isolated["clock"].t / 60 > 120


def test_the_sweep_stops_at_its_deadline_and_says_so(isolated, monkeypatch, capsys):
    total = publish_ice.DEFAULT_ROBUSTA_BUDGET_MIN * 60
    reserve = publish_ice.reserve_for(3)
    budget, result = _sweep(isolated, monkeypatch, total_s=total, reserve_s=reserve)

    assert result == (None, None)
    walked = len(isolated["requested"])
    assert 1400 < walked < 1810
    # Stopped at the first candidate past the deadline — not one GET later.
    assert total - reserve <= isolated["clock"].t < total - reserve + 4.0
    # A budget stop is not a claim about where ICE put the file.
    assert orchestrate._RUN_STATS["sweep_exhausted"] is False
    assert budget.sweep_cut["day"] == "2026-11-16"
    assert budget.sweep_cut["candidates_walked"] == walked
    assert "::warning::[budget]" in capsys.readouterr().out
    # orchestrate's own finally still ran: throttle restored, cursor persisted.
    assert orchestrate._THROTTLE["marketdata"] == 8.0
    cursor = json.loads((isolated["tmp"] / "cursor.json").read_text())
    assert cursor["date"] == "2026-11-16"


def test_no_stock_report_search_starts_after_the_sweep_deadline(isolated):
    clock = isolated["clock"]
    budget = publish_ice.Budget(100, 50, clock=clock)
    clock.t = 60
    pull = publish_ice._budgeted_stock_report(orchestrate.pull_stock_report, budget)
    assert pull(date(2026, 11, 13), sweep=False) == (None, None)
    assert isolated["requested"] == []
    assert budget.skipped["pull_stock_report"] == 1


def test_per_day_pulls_return_empty_after_the_hard_deadline(isolated):
    clock = isolated["clock"]
    budget = publish_ice.Budget(100, 50, clock=clock)
    gradings = publish_ice._budgeted_pull("pull_gradings", orchestrate.pull_gradings, budget)
    tenders = publish_ice._budgeted_pull("pull_tenders", orchestrate.pull_tenders, budget)
    gradings(date(2026, 11, 17))
    assert isolated["requested"], "before the deadline the real pull runs"
    isolated["requested"].clear()
    clock.t = 100
    assert gradings(date(2026, 11, 17)) == []
    assert tenders(date(2026, 11, 17)) == (None, None)
    assert isolated["requested"] == []


# ── holes 620 can never publish are not fetched ──────────────────────────────

def test_hit_log_days_before_the_window_are_held_not_refetched(isolated):
    isolated["write_hits"]([("bootstrap", "103021"), ("2026-06-03", "103100"),
                            ("2026-11-12", "103459"), ("2026-11-13", "103551")])
    held = publish_ice.held_upstream_dates(orchestrate, date(2026, 11, 13))
    assert held == ["2026-06-03", "2026-11-12"]


def test_the_window_mirrors_runs_anchor_rule():
    morning = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)          # Wednesday
    assert publish_ice.window_days(orchestrate, 3, morning) == [
        date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17)]
    evening = datetime(2026, 11, 18, 19, 0, tzinfo=UTC)
    assert publish_ice.window_days(orchestrate, 3, evening)[-1] == date(2026, 11, 18)


# ── the passes, end to end through the real run() ────────────────────────────

def _old_hits(n=111):
    from datetime import timedelta
    start = date(2026, 6, 3)
    return [((start + timedelta(days=i)).isoformat(), "103100") for i in range(n)]


def test_the_arabica_pass_sends_no_robusta_request(isolated, monkeypatch):
    isolated["write_hits"](_old_hits())
    publish_ice.run_pass("arabica", 3)
    urls = isolated["requested"]
    assert urls, "the arabica pass must still fetch arabica"
    assert all("/liffe/" not in u for u in urls), [u for u in urls if "/liffe/" in u][:3]
    # Every attribute swapped for the pass is put back.
    assert orchestrate.pull_stock_report.__name__ == "pull_stock_report"
    assert orchestrate.OUT_DIR == isolated["tmp"] / "orig_stage"


def test_the_robusta_pass_skips_ancient_holes_and_sends_no_arabica_request(isolated, monkeypatch, capsys):
    """The 111-hole case: every hit-log date was a 'hole' because 620's stage is
    empty each run. None before the window may be requested."""
    isolated["write_hits"](_old_hits())
    window = publish_ice.window_days(orchestrate, 3)
    total = publish_ice.DEFAULT_ROBUSTA_BUDGET_MIN * 60
    budget = publish_ice.Budget(total, publish_ice.reserve_for(3), clock=isolated["clock"])
    publish_ice.run_pass("robusta", 3, budget=budget)

    urls = isolated["requested"]
    assert urls
    assert all("futures_us_reports" not in u for u in urls)
    stock_days = {u.split("Stock_Report_RC_")[1][:8] for u in urls if "Stock_Report_RC_" in u}
    assert stock_days, "the robusta pass must still search the window"
    assert min(stock_days) >= window[0].strftime("%Y%m%d"), sorted(stock_days)[:3]
    assert "treated as held" in capsys.readouterr().out
    assert isolated["clock"].t <= total + 30, "the pass overran its own budget"


def test_arabica_is_published_before_the_robusta_pass_starts(isolated, monkeypatch):
    """Ordering, and its point: a robusta pass that dies still leaves arabica on
    disk for the workflow to have committed."""
    events: list[str] = []
    stage = isolated["tmp"] / "_stage"
    out = isolated["tmp"] / "data" / "ice"
    today = datetime.now(UTC).date().isoformat()

    def fake_run_pass(which, days, *, budget=None):
        events.append(f"fetch {which}")
        if which == "robusta":
            assert (out / "certified_stocks_arabica_latest.json").is_file(), \
                "robusta started before arabica was published"
            raise RuntimeError("job timeout")
        (stage / which).mkdir(parents=True, exist_ok=True)
        (stage / which / "certified_stocks_arabica.json").write_text(
            json.dumps({"snapshots": [{"date": today, "total_bags": 1}]}))
        return {}

    real_publish = publish_ice.publish

    def spy_publish(cutoff, dry_run=False, **kw):
        events.append(f"publish {kw['markets'][0]}")
        return real_publish(cutoff, dry_run, **kw)

    monkeypatch.setattr(publish_ice, "run_pass", fake_run_pass)
    monkeypatch.setattr(publish_ice, "publish", spy_publish)
    with pytest.raises(RuntimeError):
        publish_ice.fetch_and_publish(("robusta", "arabica"), 3, "2026-01-01")

    assert events == ["fetch arabica", "publish certified_stocks_arabica", "fetch robusta"]
    assert json.loads((out / "certified_stocks_arabica_latest.json").read_text())["snapshots"]
    assert not (out / "certified_stocks_robusta_latest.status.json").exists(), \
        "the arabica pass must not write a robusta failure status"


def test_the_budget_is_recorded_in_the_robusta_status(isolated, monkeypatch):
    stage = isolated["tmp"] / "_stage" / "robusta"
    today = datetime.now(UTC).date().isoformat()

    def fake_run_pass(which, days, *, budget=None):
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "certified_stocks_robusta.json").write_text(json.dumps(
            {"snapshots": [{"date": today, "by_port_lots": {"ANR": 1}}]}))
        return {"_budget": budget.summary()}

    monkeypatch.setattr(publish_ice, "run_pass", fake_run_pass)
    assert publish_ice.fetch_and_publish(("robusta",), 3, "2026-01-01") == 0
    status = json.loads((isolated["tmp"] / "data" / "ice"
                         / "certified_stocks_robusta_latest.status.json").read_text())
    assert status["budget"]["total_min"] == publish_ice.DEFAULT_ROBUSTA_BUDGET_MIN


# ── the workflow wires it the same way ───────────────────────────────────────

def _steps():
    doc = yaml.safe_load(FETCH.read_text())
    return doc["jobs"]["fetch"], doc["jobs"]["fetch"]["steps"]


def test_arabica_is_committed_before_the_robusta_fetch_starts():
    _, steps = _steps()
    names = [s.get("name") for s in steps]
    assert names.index("Fetch and publish arabica") < names.index("Commit the arabica payload") \
        < names.index("Fetch and publish robusta") < names.index("Commit the payload")


def test_the_robusta_budget_fits_inside_its_step_and_the_job():
    job, steps = _steps()
    by_name = {s.get("name"): s for s in steps}
    arabica, robusta = by_name["Fetch and publish arabica"], by_name["Fetch and publish robusta"]
    budget = float(robusta["run"].split("--budget-min")[1].split()[0])
    assert budget < robusta["timeout-minutes"], "the step timeout must be a backstop only"
    # + setup and the two commits, which take a minute or two between them.
    assert arabica["timeout-minutes"] + robusta["timeout-minutes"] + 5 <= job["timeout-minutes"]


def test_the_fetch_steps_are_unbuffered():
    _, steps = _steps()
    for s in steps:
        if (s.get("name") or "").startswith("Fetch and publish"):
            assert "python3 -u" in s["run"]
            assert s["env"]["PYTHONUNBUFFERED"] == "1"


def test_robusta_still_runs_when_arabica_fails():
    _, steps = _steps()
    robusta = next(s for s in steps if s.get("name") == "Fetch and publish robusta")
    assert "!cancelled()" in robusta["if"]
