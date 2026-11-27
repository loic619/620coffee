"""Cohort-DNA implied-outflow algorithm — pure-function tests.

These mirror the worked example from the design doc: monthly ageing
reports, cohort DNA from gradings events, per-cohort shrinkage, and
the implied per-origin outflow that falls out.

Adapted from 619's backend/scraper/tests/test_cohort_outflow.py as of 619
commit f73b68f5 (2026-11-24), which added build_current_by_origin_method and
its three tests at the end of this file. cohort_outflow.py is byte-identical
in both repos (fetch/PORTING.md), so the tests are too; only the module
docstring and the sys.path anchor differ, since the package lives under
fetch/ here.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetch"))

from scraper.sources.ice_certified_stocks.cohort_outflow import (  # noqa: E402
    build_cohort_dna,
    build_current_by_origin,
    build_current_by_origin_method,
    build_implied_outflow,
    decompose_report_to_port_cohort_lots,
    shift_month_iso,
)

# ── Month arithmetic ─────────────────────────────────────────────────────────

def test_shift_month_iso_back():
    assert shift_month_iso("2026-02", -1) == "2026-01"
    assert shift_month_iso("2026-02", -2) == "2025-12"
    assert shift_month_iso("2026-02", -14) == "2024-12"


def test_shift_month_iso_forward():
    assert shift_month_iso("2025-11", 3) == "2026-02"


# ── Cohort decomposition ────────────────────────────────────────────────────

def test_decompose_report_to_port_cohort_lots_basic():
    # ME=2026-02-28 with two buckets at port ANT:
    #   k=1 (cohort 2026-01): 1000 MT  → 100 lots
    #   k=5 (cohort 2025-09): 200 MT   → 20 lots
    rep = {
        "month_end": "2026-02-28",
        "valid": {"buckets": [
            {"months_since_graded": 1, "by_port": {"ANT": 1000}},
            {"months_since_graded": 5, "by_port": {"ANT": 200}},
        ]},
    }
    got = decompose_report_to_port_cohort_lots(rep)
    assert got == {"ANT": {"2026-01": 100.0, "2025-09": 20.0}}


# ── Cohort DNA ───────────────────────────────────────────────────────────────

def test_build_cohort_dna_normalises_per_port_per_month():
    raw = {
        "ANT": {
            "2026-02": {"Vietnam": 75, "Indonesia": 25},
            "2025-09": {"Indonesia": 100},
        },
        "LON": {"2026-02": {"Brazilian Conillon": 50, "Vietnam": 50}},
    }
    dna = build_cohort_dna(raw)
    assert dna["ANT"]["2026-02"]["Vietnam"] == 0.75
    assert dna["ANT"]["2026-02"]["Indonesia"] == 0.25
    assert dna["ANT"]["2025-09"]["Indonesia"] == 1.0
    assert dna["LON"]["2026-02"]["Vietnam"] == 0.5


def test_build_cohort_dna_drops_zero_cohorts():
    raw = {"ANT": {"2026-01": {"Vietnam": 0}, "2026-02": {"Vietnam": 10}}}
    dna = build_cohort_dna(raw)
    assert "2026-01" not in dna["ANT"]
    assert dna["ANT"]["2026-02"]["Vietnam"] == 1.0


# ── User's worked example: 90 lots remain after a 100-lot Feb grading ──────

def test_implied_outflow_user_example():
    """RP1 (Jan) → RP2 (Feb) — mirrors the design-doc walkthrough.

    Jan 31 ageing report (RP1) at ANT:
        10 lots from 4mo-old   (cohort 2025-09 — 100% Indonesia)
        10 lots from 12mo-old  (cohort 2025-01 — 50% Vietnam / 50% Indonesia)

    Feb gradings (GP2): 100 lots at ANT, 75% Vietnam / 25% Indonesia.
    Feb 28 ageing report (RP2) at ANT:
         8 lots from 5mo-old   (cohort 2025-09 — was 10, lost 2)
         2 lots from 13mo-old  (cohort 2025-01 — was 10, lost 8)
        80 lots from 1mo-old   (cohort 2026-02 — was 100, lost 20)
                  Note: the source has months_since_graded ≥ 1 only, so
                  the "1mo-old" bucket on the Feb 28 report IS the Feb
                  cohort that was just graded — the 0-mo case never
                  appears in this exchange's data, the new cohort just
                  shows up as k=1 in the following month-end report.

    Expected per-origin implied outflow during February:
      • cohort 2025-09 shrank by 2 lots → 2 × 100% Indonesia = 2.0 Indo
      • cohort 2025-01 shrank by 8 lots → 8 × 50%/50%       = 4.0 Vietnam + 4.0 Indo
      • cohort 2026-02 first-seen 80 vs graded 100 → 20 lots shrank
        → 20 × 75%/25% = 15.0 Vietnam + 5.0 Indo
      Total: Vietnam ≈ 19.0  ·  Indonesia ≈ 11.0
    """
    # NB the user's worked example assumes a k=0 bucket for the current-
    # month cohort (her "0 month old" line). The ICE source we actually
    # parse omits k=0 and the cohort first surfaces at k=1 in the *next*
    # month-end report; the algorithm handles both — this test mirrors
    # the user's hypothetical source so the math lines up exactly with
    # the worked numbers in the design doc.
    age_reports = [
        # Jan 31 ageing report
        {"month_end": "2026-01-31", "valid": {"buckets": [
            {"months_since_graded": 4,  "by_port": {"ANT": 100}},  # 10 lots, cohort 2025-09
            {"months_since_graded": 12, "by_port": {"ANT": 100}},  # 10 lots, cohort 2025-01
        ]}},
        {"month_end": "2026-02-28", "valid": {"buckets": [
            {"months_since_graded": 0,  "by_port": {"ANT": 800}},  # 80 lots, cohort 2026-02 (new)
            {"months_since_graded": 5,  "by_port": {"ANT": 80}},   # 8 lots, cohort 2025-09
            {"months_since_graded": 13, "by_port": {"ANT": 20}},   # 2 lots, cohort 2025-01
        ]}},
    ]
    # Cohort DNA (gradings events of each calendar month)
    gradings_raw = {
        "ANT": {
            "2025-09": {"Indonesia": 50},
            "2025-01": {"Vietnam": 30, "Indonesia": 30},
            "2026-02": {"Vietnam": 75, "Indonesia": 25},
        },
    }
    cohort_dna = build_cohort_dna(gradings_raw)
    out = build_implied_outflow(age_reports, cohort_dna, port_alltime_dna={}, gradings_per_port_month_origin=gradings_raw)
    assert len(out) == 1
    feb = out[0]
    assert feb["month_end"] == "2026-02-28"
    feb_ant = feb["by_port"]["ANT"]
    assert abs(feb_ant["Vietnam"]   - 19.0) < 0.01, f"Vietnam outflow = {feb_ant['Vietnam']}"
    assert abs(feb_ant["Indonesia"] - 11.0) < 0.01, f"Indonesia outflow = {feb_ant['Indonesia']}"

    # in & out subset = the Feb cohort (2026-02) shrinkage only: graded 100,
    # 80 remain → 20 lots arrived AND left → 20 × 75%/25% = 15 Vn + 5 Indo.
    # The legacy cohorts (2025-09, 2025-01) are pure out, not transit.
    feb_transit = feb["by_port_transit"]["ANT"]
    assert abs(feb_transit["Vietnam"]   - 15.0) < 0.01, f"Vietnam in&out = {feb_transit['Vietnam']}"
    assert abs(feb_transit["Indonesia"] -  5.0) < 0.01, f"Indonesia in&out = {feb_transit['Indonesia']}"
    # pure out = total − transit
    assert abs((feb_ant["Vietnam"]   - feb_transit["Vietnam"])   - 4.0) < 0.01
    assert abs((feb_ant["Indonesia"] - feb_transit["Indonesia"]) - 6.0) < 0.01


def test_implied_outflow_no_transit_when_only_legacy_cohorts():
    """A window where only old cohorts shrink has zero in & out."""
    age_reports = [
        {"month_end": "2026-01-31", "valid": {"buckets": [
            {"months_since_graded": 24, "by_port": {"ANT": 100}},  # cohort 2024-01
        ]}},
        {"month_end": "2026-02-28", "valid": {"buckets": [
            {"months_since_graded": 25, "by_port": {"ANT": 60}},   # cohort 2024-01
        ]}},
    ]
    out = build_implied_outflow(
        age_reports, cohort_dna={}, port_alltime_dna={"ANT": {"Vietnam": 1.0}},
        gradings_per_port_month_origin={},
    )
    assert out[0]["by_port"]["ANT"]["Vietnam"] > 0
    # No newly-graded cohort left in this interval → empty transit map.
    assert out[0]["by_port_transit"] == {}


def test_implied_outflow_falls_back_to_port_alltime_dna():
    # Cohort that the gradings dict doesn't cover (predates our gradings
    # event history). Buckets are MT; 100 MT → 10 lots, 60 MT → 6 lots.
    age_reports = [
        {"month_end": "2026-01-31", "valid": {"buckets": [
            {"months_since_graded": 24, "by_port": {"ANT": 100}},  # cohort 2024-01
        ]}},
        {"month_end": "2026-02-28", "valid": {"buckets": [
            {"months_since_graded": 25, "by_port": {"ANT": 60}},   # cohort 2024-01
        ]}},
    ]
    cohort_dna: dict = {}
    port_alltime_dna = {"ANT": {"Vietnam": 0.6, "Indonesia": 0.4}}
    out = build_implied_outflow(
        age_reports, cohort_dna, port_alltime_dna,
        gradings_per_port_month_origin={},
    )
    feb_ant = out[0]["by_port"]["ANT"]
    # shrinkage = 10 − 6 = 4 lots → apportioned 60/40
    assert abs(feb_ant["Vietnam"]   - 4 * 0.6) < 0.01
    assert abs(feb_ant["Indonesia"] - 4 * 0.4) < 0.01


# ── Current per-origin breakdown ────────────────────────────────────────────

def test_build_current_by_origin_apportions_each_bucket():
    latest = {"month_end": "2026-02-28", "valid": {"buckets": [
        {"months_since_graded": 1,  "by_port": {"ANT": 800}},   # 80 lots, cohort 2026-01
        {"months_since_graded": 12, "by_port": {"ANT": 200}},   # 20 lots, cohort 2025-02
    ]}}
    dna = build_cohort_dna({
        "ANT": {
            "2026-01": {"Vietnam": 50, "Indonesia": 50},  # 50/50
            "2025-02": {"Indonesia": 10},                  # 100% Indonesia
        },
    })
    cur = build_current_by_origin(latest, dna, port_alltime_dna={})
    assert abs(cur["ANT"]["Vietnam"]   - 40.0) < 0.01  # 80 × 50%
    assert abs(cur["ANT"]["Indonesia"] - 60.0) < 0.01  # 80 × 50% + 20 × 100%


def test_build_current_by_origin_fallback_to_alltime():
    latest = {"month_end": "2026-02-28", "valid": {"buckets": [
        {"months_since_graded": 36, "by_port": {"ANT": 100}},  # cohort 2023-02 — no DNA
    ]}}
    dna: dict = {}
    fallback = {"ANT": {"Vietnam": 0.7, "Indonesia": 0.3}}
    cur = build_current_by_origin(latest, dna, port_alltime_dna=fallback)
    assert abs(cur["ANT"]["Vietnam"]   - 7.0) < 0.01
    assert abs(cur["ANT"]["Indonesia"] - 3.0) < 0.01


def test_build_current_by_origin_method_splits_lots_by_allocation():
    # 80 lots with cohort DNA, 100 lots on the port fallback, 30 lots at a
    # port with no DNA at all (→ "?" / unallocated).
    latest = {"month_end": "2026-02-28", "valid": {"buckets": [
        {"months_since_graded": 1,  "by_port": {"ANT": 800, "LON": 300}},
        {"months_since_graded": 36, "by_port": {"ANT": 1000}},
    ]}}
    dna = build_cohort_dna({"ANT": {"2026-01": {"Vietnam": 50, "Indonesia": 50}}})
    fallback = {"ANT": {"Vietnam": 0.7, "Indonesia": 0.3}}
    gpmo = {"ANT": {"2026-01": {"Vietnam": 50, "Indonesia": 50}}}
    m = build_current_by_origin_method(latest, dna, fallback, gpmo)
    assert m["month_end"] == "2026-02-28"
    assert m["total_lots"] == 210.0
    assert m["by_method"] == {"cohort_dna": 80.0, "port_fallback": 100.0, "unallocated": 30.0}
    assert m["by_origin"]["Vietnam"] == {"cohort_dna": 40.0, "port_fallback": 70.0}
    assert m["by_origin"]["?"] == {"unallocated": 30.0}
    # Same report, same total as current_by_origin — the two never disagree.
    cur = build_current_by_origin(latest, dna, fallback, gpmo)
    assert abs(sum(sum(o.values()) for o in cur.values()) - m["total_lots"]) < 0.05


def test_build_current_by_origin_method_thin_gradings_fall_back():
    # Gradings cover 10 of 80 lots (< 50 %) → the cohort DNA is distrusted.
    latest = {"month_end": "2026-02-28", "valid": {"buckets": [
        {"months_since_graded": 1, "by_port": {"ANT": 800}},
    ]}}
    gpmo = {"ANT": {"2026-01": {"Vietnam": 10}}}
    dna = build_cohort_dna(gpmo)
    m = build_current_by_origin_method(latest, dna, {"ANT": {"Indonesia": 1.0}}, gpmo)
    assert m["by_method"]["cohort_dna"] == 0.0
    assert m["by_method"]["port_fallback"] == 80.0


def test_build_current_by_origin_method_empty_report():
    assert build_current_by_origin_method({}, {}, {}) == {}
