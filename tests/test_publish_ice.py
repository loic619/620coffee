"""What 620 is allowed to publish.

620 is public and a publish cannot be undone, so these tests are about refusal:
the allow-list drops what it does not name, the window trim removes accumulated
history, and the snapshot rail refuses outright rather than writing something
too large. Each has a corresponding real-world near-miss recorded in the
comments — none of these are hypothetical.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetch"))

from allowlist import ICE_ARABICA, ICE_ROBUSTA, prune  # noqa: E402

sys.path.insert(0, str(ROOT))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("publish_ice", ROOT / "fetch" / "publish_ice.py")
publish_ice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish_ice)


# ── the allow-list drops what it does not name ───────────────────────────────

def test_the_engine_ratchet_is_dropped():
    kept, dropped = prune({"as_of": "2026-09-04", "port_peaks": {"LON": 1}}, ICE_ARABICA)
    assert "port_peaks" not in kept
    assert "port_peaks" in dropped


def test_cohort_derivations_are_dropped_but_raw_monthly_reports_survive():
    doc = {"monthly": {
        "iss_recv_monthly": [{"month": "2026-08"}],   # ICE publishes this
        "age_allowance": [{"month_end": "2026-08-31"}],  # and this
        "implied_outflow": [{"month_end": "2026-08-31"}],  # cohort_outflow.py
        "current_by_origin": {"BR": 1},                    # cohort DNA
    }}
    kept, dropped = prune(doc, ICE_ROBUSTA)
    assert sorted(kept["monthly"]) == ["age_allowance", "iss_recv_monthly"]
    assert "monthly.implied_outflow" in dropped
    assert "monthly.current_by_origin" in dropped


def test_accumulated_port_history_is_dropped():
    kept, dropped = prune({"port_origin_history": {"LON": [1, 2, 3]}}, ICE_ROBUSTA)
    assert "port_origin_history" not in kept
    assert "port_origin_history" in dropped


def test_an_unanticipated_field_cannot_leak():
    """The reason this is an allow-list and not a deny-list: a field nobody
    thought about must be dropped, not published."""
    doc = {"snapshots": [{"date": "2026-09-04", "total_bags": 10,
                          "some_new_engine_score": 0.91}]}
    kept, dropped = prune(doc, ICE_ARABICA)
    assert "some_new_engine_score" not in kept["snapshots"][0]
    assert kept["snapshots"][0]["total_bags"] == 10
    assert any("some_new_engine_score" in d for d in dropped)


@pytest.mark.parametrize("field", ["failed_by_origin", "passed_by_origin",
                                   "graded_today_by_port_origin", "by_group"])
def test_raw_report_detail_survives(field):
    """Over-strict is a bug too: these are ICE's own report fields and the
    frontend reads them."""
    kept, _ = prune({"snapshots": [{"date": "d", field: {"BR": 1}}]}, ICE_ARABICA)
    assert field in kept["snapshots"][0]


# ── the window trim removes accumulated history ──────────────────────────────

def test_records_older_than_the_window_are_trimmed():
    """A three-snapshot robusta run was measured carrying 152 tender records
    reaching back fifteen months, because the parser accumulates them."""
    doc = {"recent_activity": {"tenders": [
        {"date": "2025-06-25", "lots": 1},
        {"date": "2026-09-05", "lots": 2},
        {"date": "2026-09-07", "lots": 3},
    ]}}
    kept, removed = publish_ice.trim_to_window(doc, "2026-08-26")
    assert [t["date"] for t in kept["recent_activity"]["tenders"]] == ["2026-09-05", "2026-09-07"]
    assert removed["recent_activity.tenders"] == 1


def test_a_record_with_no_date_is_kept():
    """Guessing that an undated record is old would silently drop live data."""
    doc = {"recent_activity": {"tenders": [{"lots": 1}]}}
    kept, removed = publish_ice.trim_to_window(doc, "2026-08-26")
    assert kept["recent_activity"]["tenders"] == [{"lots": 1}]
    assert not removed


def test_month_end_records_are_recognised_as_dated():
    doc = {"monthly": {"age_allowance": [{"month_end": "2024-01-31"},
                                         {"month_end": "2026-09-01"}]}}
    kept, removed = publish_ice.trim_to_window(doc, "2026-08-26")
    assert len(kept["monthly"]["age_allowance"]) == 1
    assert removed["monthly.age_allowance"] == 1


# ── the rail refuses rather than publishing history ──────────────────────────

def test_publishing_a_history_sized_payload_is_refused(tmp_path, monkeypatch):
    """619's real arabica file has 322 snapshots. If a misconfiguration ever
    staged one, 620 must refuse: a public repository cannot un-publish."""
    stage = tmp_path / "_stage"
    out = tmp_path / "data" / "ice"
    stage.mkdir(parents=True)
    out.mkdir(parents=True)
    monkeypatch.setattr(publish_ice, "STAGE", stage)
    monkeypatch.setattr(publish_ice, "OUT", out)
    # The real catalogue lives in the repository. A test that publishes must not
    # write to it — an earlier version of this file did, and committed a
    # catalogue entry for a payload that only ever existed in a tmpdir.
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"catalog_version": 1, "generated_at": "x", "datasets": []}')
    monkeypatch.setattr(publish_ice, "CATALOG", catalog)

    history = {"as_of": "2026-09-04", "snapshots": [
        {"date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "total_bags": i} for i in range(100)]}
    (stage / "certified_stocks_arabica.json").write_text(json.dumps(history))
    (stage / "certified_stocks_robusta.json").write_text(json.dumps({"snapshots": []}))

    failures = publish_ice.publish("2020-01-01")

    assert failures >= 1
    assert not (out / "certified_stocks_arabica_latest.json").exists(), \
        "a history-sized payload must not be written"
    status = json.loads((out / "certified_stocks_arabica_latest.status.json").read_text())
    assert status["ok"] is False
    assert "rail" in status["error"]


def test_a_normal_window_publishes(tmp_path, monkeypatch):
    stage = tmp_path / "_stage"
    out = tmp_path / "data" / "ice"
    stage.mkdir(parents=True)
    out.mkdir(parents=True)
    monkeypatch.setattr(publish_ice, "STAGE", stage)
    monkeypatch.setattr(publish_ice, "OUT", out)
    # The real catalogue lives in the repository. A test that publishes must not
    # write to it — an earlier version of this file did, and committed a
    # catalogue entry for a payload that only ever existed in a tmpdir.
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"catalog_version": 1, "generated_at": "x", "datasets": []}')
    monkeypatch.setattr(publish_ice, "CATALOG", catalog)

    window = {"as_of": "2026-09-04", "port_peaks": {"NY": 5}, "snapshots": [
        {"date": "2026-09-02", "total_bags": 1},
        {"date": "2026-09-03", "total_bags": 2},
        {"date": "2026-09-04", "total_bags": 3}]}
    (stage / "certified_stocks_arabica.json").write_text(json.dumps(window))
    (stage / "certified_stocks_robusta.json").write_text(json.dumps({"snapshots": []}))

    publish_ice.publish("2026-08-26")

    payload = json.loads((out / "certified_stocks_arabica_latest.json").read_text())
    status = json.loads((out / "certified_stocks_arabica_latest.status.json").read_text())
    assert len(payload["snapshots"]) == 3
    assert "port_peaks" not in payload
    assert status["ok"] is True
    assert status["window"] == {"start": "2026-09-02", "end": "2026-09-04"}
    assert "port_peaks" in status["dropped_fields"]


def test_an_empty_fetch_does_not_overwrite_a_good_payload(tmp_path, monkeypatch):
    """A failed fetch must not replace a good window with an empty one."""
    stage = tmp_path / "_stage"
    out = tmp_path / "data" / "ice"
    stage.mkdir(parents=True)
    out.mkdir(parents=True)
    monkeypatch.setattr(publish_ice, "STAGE", stage)
    monkeypatch.setattr(publish_ice, "OUT", out)
    # The real catalogue lives in the repository. A test that publishes must not
    # write to it — an earlier version of this file did, and committed a
    # catalogue entry for a payload that only ever existed in a tmpdir.
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"catalog_version": 1, "generated_at": "x", "datasets": []}')
    monkeypatch.setattr(publish_ice, "CATALOG", catalog)

    good = {"snapshots": [{"date": "2026-09-04", "total_bags": 3}]}
    (out / "certified_stocks_arabica_latest.json").write_text(json.dumps(good))
    (stage / "certified_stocks_arabica.json").write_text(json.dumps({"snapshots": []}))
    (stage / "certified_stocks_robusta.json").write_text(json.dumps({"snapshots": []}))

    failures = publish_ice.publish("2026-08-26")

    assert failures == 2
    assert json.loads((out / "certified_stocks_arabica_latest.json").read_text()) == good
