"""Two fixes ported from 619's acaphe_poller.py, with 619's own tests.

- 619 23c900713 (2026-10-27): a missing or empty price cell is ABSENT (None),
  not a silent 0.0. The old `str(s or "0")` coerced it to zero, which let an
  unpriced board through classify() (gated on `last is not None`) and could
  publish an all-zeros live_quotes as "healthy".
- 619 dd1cc3e4e (2026-10-25): the VCB USD/VND quote date is parsed into
  `usd_vnd_date`, and a rate outside 10,000-40,000 is dropped as a parse
  artefact rather than published.

Each group carries a positive control: a genuine value ("0", an in-band rate)
must still come through, so a test cannot pass by the parser returning None
for everything.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fetch"))

from scraper import acaphe_poller as m  # noqa: E402

# ── _safe_float/_safe_int: missing ≠ zero (619 23c900713) ────────────────────


def test_safe_float_absent_is_none_not_zero():
    # An absent cell must be None, so classify() can tell "unpriced" apart from
    # a legitimate 0 — coercing "" → 0.0 was a hole straight through the gate.
    assert m._safe_float("") is None
    assert m._safe_float(None) is None
    assert m._safe_float("   ") is None
    # Positive control: a genuine value (including 0) still parses.
    assert m._safe_float("3,406") == 3406.0
    assert m._safe_float("0") == 0.0


def test_safe_int_absent_is_none_not_zero():
    assert m._safe_int("") is None
    assert m._safe_int(None) is None
    # Positive control.
    assert m._safe_int("1,234") == 1234
    assert m._safe_int("0") == 0


def test_offsession_unpriced_payload_does_not_pass_as_healthy():
    """acaphe's off-session shape — rows present, every price cell empty — must
    classify as degraded, so the last-good snapshot is kept rather than
    overwritten with an all-zeros board."""
    raw = [
        {"stt": 1, "Month": "RCX26", "Last": "", "High": "", "Low": "",
         "Open": "", "Prev": "", "Change": "", "Change_per": "", "OpInt": "",
         "Time": "", "Vol": ""},
        {"stt": 1, "Month": "ARAZ26", "Last": "", "High": "", "Low": "",
         "Open": "", "Prev": "", "Change": "", "Change_per": "", "OpInt": "",
         "Time": "", "Vol": ""},
    ]
    out = m.transform(raw)
    assert out["robusta"][0]["last"] is None
    assert out["arabica"][0]["last"] is None
    assert out["robusta"][0]["vol"] is None
    status, reasons = m.classify(out)
    assert status == "degraded"
    assert reasons == ["robusta: 1 rows, none priced", "arabica: 1 rows, none priced"]


def test_priced_payload_still_classifies_healthy():
    """Positive control for the test above: the same shape with prices filled
    in must still publish, so 'degraded' there is about the empty cells."""
    raw = [
        {"stt": 1, "Month": "RCX26", "Last": "4,512", "High": "4,530",
         "Low": "4,480", "Open": "4,500", "Prev": "4,490", "Change": "22",
         "Change_per": "(0.49%)", "OpInt": "", "Time": "", "Vol": "1,200"},
        {"stt": 1, "Month": "ARAZ26", "Last": "389.50", "High": "391.00",
         "Low": "386.10", "Open": "387.00", "Prev": "388.00", "Change": "1.5",
         "Change_per": "(0.39%)", "OpInt": "", "Time": "", "Vol": "900"},
    ]
    out = m.transform(raw)
    assert out["robusta"][0]["last"] == 4512.0
    assert out["robusta"][0]["vol"] == 1200
    assert m.classify(out)[0] == "healthy"


# ── VCB USD/VND rate: date capture + plausibility guard (619 dd1cc3e4e) ──────


def _row14(low: str) -> dict:
    """Minimal row14 carrying only the Low cell the FX rate lives in."""
    return {"High": "", "Low": low}


def test_vcb_date_resolves_to_most_recent_past_year():
    # Jul 15 seen in late Oct 2026 → this year.
    assert m._parse_vcb_date("26,070 (11:26 Jul 15)", today=date(2026, 10, 25)) == "2026-07-15"
    # Dec 20 seen in early Jan 2027 → last year, the most recent past occurrence.
    assert m._parse_vcb_date("25,900 (09:00 Dec 20)", today=date(2027, 1, 5)) == "2026-12-20"


def test_vcb_date_is_none_when_absent_or_garbled():
    assert m._parse_vcb_date("26,070", today=date(2026, 10, 25)) is None
    assert m._parse_vcb_date("26,070 (Zzz 99)", today=date(2026, 10, 25)) is None


def test_parse_vietnam_keeps_number_and_captures_date():
    # Positive control for the guard below: an in-band rate is kept.
    vn = m._parse_vietnam(_row14("26,070 (11:26 Jul 15)"))
    assert vn["usd_vnd"] == 26070                  # number unchanged in the comma case
    assert vn["usd_vnd_date"] is not None
    assert vn["usd_vnd_date"].endswith("-07-15")   # Jul 15, year resolved to most recent past


def test_parse_vietnam_rejects_implausible_rate(capsys):
    # A decimal-formatted "8.7794" has its dot stripped by re.sub(\D) → 87794,
    # which is out of band. The guard drops both the number and its date.
    vn = m._parse_vietnam(_row14("8.7794 (11:26 Jul 15)"))
    assert vn["usd_vnd"] is None
    assert vn["usd_vnd_date"] is None
    assert "implausible USD/VND 87794" in capsys.readouterr().out
    # A raw out-of-band integer is rejected the same way.
    assert m._parse_vietnam(_row14("87794 (11:26 Jul 15)"))["usd_vnd"] is None


def test_parse_vietnam_garbled_date_keeps_number():
    vn = m._parse_vietnam(_row14("26,070"))
    assert vn["usd_vnd"] == 26070
    assert vn["usd_vnd_date"] is None
