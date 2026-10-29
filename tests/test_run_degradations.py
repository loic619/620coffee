"""run_degradations.record keeps every degradation a run files.

It used to key rows on the GitHub run id alone, so the second degradation a
run filed (CEPEA unreadable, then a restated quote in the same scraper run)
overwrote the first, and the data-map panel showed one skip where there were
two. Against that code `test_two_degradations_in_one_run_both_survive` fails
with one row instead of two — the positive control for this file.

Mirrors 619's backend/scraper/tests/test_run_degradations.py; only the import
path differs. run_degradations.py itself is byte-identical with 619's copy.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fetch"))

from scraper import run_degradations as rd


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "run_degradations.json"
    monkeypatch.setattr(rd, "PATH", p)
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    return p


def _runs(p):
    return json.loads(p.read_text())["runs"]


def _cepea():
    rd.record(workflow="Scraper", file="scraper-daily.yml", kind="cepea_unreadable",
              detail="CEPEA not written", items=["Arabica", "Robusta"])


def _restated(sym):
    rd.record(workflow="Scraper", file="scraper-daily.yml", kind="restated_value",
              detail=f"{sym} frozen", items=[sym])


def test_two_degradations_in_one_run_both_survive(path):
    _cepea()
    _restated("VN_FAQ")
    rows = _runs(path)
    assert sorted(r["kind"] for r in rows) == ["cepea_unreadable", "restated_value"]
    assert {r["run_id"] for r in rows} == {"4242"}


def test_same_kind_for_two_items_both_survive(path):
    _restated("VN_FAQ")
    _restated("UGA_S15")
    assert sorted(r["items"][0] for r in _runs(path)) == ["UGA_S15", "VN_FAQ"]


def test_same_degradation_twice_in_one_attempt_is_one_row(path):
    _cepea()
    _cepea()
    assert len(_runs(path)) == 1


def test_a_retried_attempt_replaces_the_earlier_attempts_rows(path, monkeypatch):
    _cepea()
    _restated("VN_FAQ")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    _restated("VN_FAQ")
    rows = _runs(path)
    assert [(r["kind"], r["run_attempt"]) for r in rows] == [("restated_value", "2")]


def test_other_runs_and_legacy_rows_are_read_and_kept(path):
    # A row written before run_attempt existed (no such key) from another run.
    legacy = {"at": "2099-01-01T00:00:00+00:00", "date": "2099-01-01", "workflow": "X",
              "file": "x.yml", "run_id": "1", "run_url": None, "kind": "http_403",
              "detail": "d", "items": []}
    path.write_text(json.dumps({"generated_at": "", "keep_days": 30, "runs": [legacy]}))
    _cepea()
    rows = _runs(path)
    assert sorted(r["run_id"] for r in rows) == ["1", "4242"]
    assert any("run_attempt" not in r for r in rows)  # legacy row untouched
    doc = json.loads(path.read_text())
    assert set(doc) == {"generated_at", "keep_days", "runs"}  # envelope unchanged for readers
