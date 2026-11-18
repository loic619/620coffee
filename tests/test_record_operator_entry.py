"""The operator entry: what a person may write into 620's publish-time record.

Every rule here guards a way the record could be damaged by a typo or a wrong
judgement — and a damaged hit log poisons tier 0 for that date until someone
notices, while a contested date makes 619's sync refuse outright.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("record_operator_entry",
                                               ROOT / "fetch" / "record_operator_entry.py")
roe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(roe)

TODAY = dt.date(2026, 11, 18)  # a Wednesday
WORKFLOW = ROOT / ".github" / "workflows" / "record-ice-operator-entry.yml"


@pytest.fixture
def files(tmp_path):
    hits = tmp_path / "stock_report_hits.json"
    nr = tmp_path / "no_release_days.json"
    hits.write_text(json.dumps({"hits": [
        {"date": "bootstrap", "hhmmss": "103021"},
        {"date": "2026-11-13", "hhmmss": "103551"},
    ]}, indent=2), encoding="utf-8")
    nr.write_text(json.dumps({"note": "n", "days": [
        {"date": "2026-08-31", "reason": "UK summer bank holiday", "source": "operator",
         "recorded": "2026-09-04T16:43:45Z"},
    ]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"hits_path": hits, "no_release_path": nr, "today": TODAY}


def _hits(files):
    return json.loads(files["hits_path"].read_text())["hits"]


def _nr(files):
    return json.loads(files["no_release_path"].read_text())["days"]


# ── recording a second ───────────────────────────────────────────────────────

def test_a_second_is_appended_for_tier_0(files):
    roe.record_time("2026-11-11", "112351", **files)
    assert _hits(files)[-1] == {"date": "2026-11-11", "hhmmss": "112351", "source": "operator"}
    assert len(_hits(files)) == 3


def test_the_hit_log_keeps_orchestrates_byte_format(files):
    """Written the way orchestrate._record_stock_report_hit writes it, so an
    entry adds lines and never reformats the rest of the file."""
    roe.record_time("2026-11-11", "112351", **files)
    text = files["hits_path"].read_text()
    assert text == json.dumps(json.loads(text), indent=2)
    assert not text.endswith("\n")


def test_the_same_second_twice_is_a_no_op(files):
    before = files["hits_path"].read_text()
    msg = roe.record_time("2026-11-13", "103551", **files)
    assert "nothing to change" in msg
    assert files["hits_path"].read_text() == before


def test_a_different_second_for_a_known_day_is_refused(files):
    """The recorded second came from a 200; replacing it would also make the
    date contested in 619's sync, which refuses the whole file."""
    before = files["hits_path"].read_text()
    with pytest.raises(roe.EntryRefused, match="refusing to replace"):
        roe.record_time("2026-11-13", "110000", **files)
    assert files["hits_path"].read_text() == before


def test_a_second_withdraws_a_no_release_mark(files):
    msg = roe.record_time("2026-08-31", "104500", **files)
    assert "withdrew" in msg
    assert _nr(files) == []
    assert files["no_release_path"].read_text().endswith("\n")


@pytest.mark.parametrize("bad, why", [
    ("11235", "bad time"), ("1123510", "bad time"), ("11:23:51", "bad time"),
    ("106000", "not a wall-clock"), ("103060", "not a wall-clock"),
    ("085959", "outside 09:00-15:00"), ("150001", "outside 09:00-15:00"),
])
def test_implausible_seconds_are_refused(files, bad, why):
    with pytest.raises(roe.EntryRefused, match=why):
        roe.record_time("2026-11-11", bad, **files)


@pytest.mark.parametrize("bad, why", [
    ("2026-11-14", "Saturday"), ("2026-11-15", "Sunday"), ("2026-11-19", "future"),
    ("2026-02-30", "not a calendar date"), ("18/11/2026", "bad date"), ("", "bad date"),
])
def test_bad_dates_are_refused(files, bad, why):
    with pytest.raises(roe.EntryRefused, match=why):
        roe.record_time(bad, "103000", **files)


# ── recording no release ─────────────────────────────────────────────────────

def test_no_release_is_filed_with_its_reason(files):
    roe.record_no_release("2026-11-11", "  ICE closed —   test  ", at="2026-11-18T00:00:00Z", **files)
    row = [d for d in _nr(files) if d["date"] == "2026-11-11"][0]
    assert row == {"date": "2026-11-11", "reason": "ICE closed — test", "source": "operator",
                   "recorded": "2026-11-18T00:00:00Z"}
    assert "—" in files["no_release_path"].read_text()  # ensure_ascii=False, as 619 wrote it


def test_no_release_for_a_day_with_a_known_second_is_refused(files):
    """The existing rule: a filename we can name is proof the market was open."""
    before = files["no_release_path"].read_text()
    with pytest.raises(roe.EntryRefused, match="it did release"):
        roe.record_no_release("2026-11-13", "holiday", **files)
    assert files["no_release_path"].read_text() == before


def test_no_release_needs_a_reason(files):
    with pytest.raises(roe.EntryRefused, match="reason is required"):
        roe.record_no_release("2026-11-11", "   ", **files)
    with pytest.raises(roe.EntryRefused, match="characters"):
        roe.record_no_release("2026-11-11", "x" * 201, **files)


# ── the CLI and the market ───────────────────────────────────────────────────

def test_arabica_is_refused_with_an_explanation():
    with pytest.raises(roe.EntryRefused, match="no publish second"):
        roe.validate_market("arabica")
    assert roe.validate_market(" Robusta ") == "robusta"


@pytest.fixture
def cli(files, monkeypatch, tmp_path):
    monkeypatch.setattr(roe, "HITS", files["hits_path"])
    monkeypatch.setattr(roe, "NO_RELEASE", files["no_release_path"])
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    return out


def test_cli_needs_exactly_one_of_second_or_reason(cli, files, capsys):
    before = files["hits_path"].read_text()
    assert roe.main(["--date", "2026-11-11"]) == 1
    assert roe.main(["--date", "2026-11-11", "--hhmmss", "112351", "--reason", "x"]) == 1
    assert "exactly one" in capsys.readouterr().out
    assert files["hits_path"].read_text() == before


def test_cli_arabica_writes_nothing(cli, files):
    before = files["hits_path"].read_text()
    assert roe.main(["--market", "arabica", "--date", "2026-11-11", "--hhmmss", "112351"]) == 1
    assert files["hits_path"].read_text() == before


def test_cli_records_and_reports_the_fetch_window(cli, files):
    # Positive control for the path the workflow takes.
    assert roe.main(["--date", "2026-11-11", "--hhmmss", "112351"]) == 0
    assert _hits(files)[-1]["date"] == "2026-11-11"
    assert re.search(r"^fetch_days=\d*$", cli.read_text(), re.M)


# ── the fetch window that reaches a date ─────────────────────────────────────

NOON = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)  # anchor = Tue 11-17


@pytest.mark.parametrize("date, days", [
    ("2026-11-17", 3),    # the anchor itself: the daily minimum
    ("2026-11-11", 5),    # Wed..Tue = 5 business days
    ("2026-11-02", 12),
    ("2026-10-07", 30),   # exactly the weekly catch-up window
    ("2026-10-06", None), # one further: no run may publish it
    ("2026-09-22", None),
])
def test_fetch_days_reach_the_date(date, days):
    assert roe.fetch_days_for(date, NOON) == days


def test_fetch_days_agree_with_the_fetchers_window():
    """Pinned against orchestrate's own _biz_days_back, so a window that
    should reach the date provably does."""
    import sys
    sys.path.insert(0, str(ROOT / "fetch"))
    from scraper.sources.ice_certified_stocks import orchestrate
    anchor = dt.date(2026, 11, 17)
    for iso in ("2026-11-11", "2026-11-02", "2026-10-07"):
        n = roe.fetch_days_for(iso, NOON)
        window = orchestrate._biz_days_back(anchor, n)
        assert dt.date.fromisoformat(iso) == min(window)


# ── the workflow ─────────────────────────────────────────────────────────────

def _wf():
    doc = yaml.safe_load(WORKFLOW.read_text())
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def test_the_workflow_is_dispatch_only():
    """It has contents: write and pushes to main."""
    assert set(_wf()["on"]) == {"workflow_dispatch"}


def test_the_workflow_shares_the_fetch_lane():
    """Both commit the hits log; a fetch that checked out before an entry and
    pushed after it would race on the same lines."""
    c = _wf()["concurrency"]
    assert c == {"group": "fetch-ice-certified-stocks", "cancel-in-progress": False}


def test_inputs_never_reach_a_run_block_unquoted():
    """The reason is free text; `${{ inputs.* }}` in a run block is shell."""
    for step in _wf()["jobs"]["record"]["steps"]:
        assert "${{" not in (step.get("run") or ""), step.get("name")


def test_the_workflow_reads_no_secret():
    code = "\n".join(ln for ln in WORKFLOW.read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "secrets." not in code


def test_the_workflow_commits_both_record_files():
    run = next(s["run"] for s in _wf()["jobs"]["record"]["steps"] if s.get("name") == "Commit")
    assert "stock_report_hits.json" in run and "no_release_days.json" in run
    assert "[skip ci]" in run
