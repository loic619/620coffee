"""acaphe's no-VN placeholder is named, not reported as an unrecognised format.

Outside its Vietnamese-morning publication acaphe puts the literal stub
`acaphe()` in row14.High. Every kept run log of poll-acaphe-quotes.yml, from the
first on 2026-09-08, printed it as `VN row14.High unrecognised: 'acaphe()'`,
which reads like a parser break. It is not one: on the ticks where the VN block
is published, it parses. The fixture is reconstructed from those run logs (see
its `_provenance`).

Positive controls: the "unrecognised" diagnostic still fires for a genuinely new
wording, and the published fixture still yields its BMT bid — so the placeholder
check cannot hide a real break or swallow real VN data.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fetch"))

from scraper import acaphe_poller as ap

FIXTURE = json.loads((HERE / "fixtures" / "acaphe_iquote_row14.json").read_text())


def test_placeholder_is_named_not_unrecognised(capsys):
    data = ap.transform(FIXTURE["placeholder"])
    out = capsys.readouterr().out
    assert "unrecognised" not in out
    assert "VN block not published this tick" in out
    assert "'acaphe()'" in out
    # No VN prices, so the poller takes the absent path (inject + #262 alert).
    assert data["vietnam"]["bmt_bid"] is None
    assert data["vietnam"]["hcm_bid"] is None
    # The rest of row14 is still read.
    assert data["vietnam"]["usd_vnd"] == 25440


def test_placeholder_tick_still_classifies_futures(capsys):
    data = ap.transform(FIXTURE["placeholder"])
    assert ap.classify(data) == ("healthy", [])


def test_published_block_still_parses(capsys):
    # Positive control: VN data is not lost when acaphe publishes it.
    viet = ap.transform(FIXTURE["published"])["vietnam"]
    out = capsys.readouterr().out
    assert "unrecognised" not in out and "not published" not in out
    assert viet["bmt_bid"] == "83000-83500"
    assert viet["bmt_offer"] == "84000-84500"
    assert viet["hcm_bid"] == "84500-85000"
    assert viet["local_time"] == "11:30 11/11"


def test_new_wording_is_still_reported_unrecognised(capsys):
    # Positive control: the placeholder check must not mute the diagnostic
    # that exists to catch a real format change.
    rows = json.loads(json.dumps(FIXTURE["placeholder"]))
    rows[-1]["High"] = "11:30 11/11 Buon Ma Thuot 83000-83500"
    viet = ap.transform(rows)["vietnam"]
    out = capsys.readouterr().out
    assert "VN row14.High unrecognised" in out
    assert "not published" not in out
    assert viet["bmt_bid"] is None


def test_placeholder_match_is_exact_not_a_substring(capsys):
    # A real block that merely mentions the stub is not the placeholder.
    rows = json.loads(json.dumps(FIXTURE["published"]))
    rows[-1]["High"] = "acaphe() " + rows[-1]["High"]
    viet = ap.transform(rows)["vietnam"]
    out = capsys.readouterr().out
    assert "not published" not in out
    assert viet["bmt_bid"] == "83000-83500"
