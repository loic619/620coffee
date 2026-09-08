"""Robusta stock report (CSV) — daily per-port lots.

Source URL: …/stock_reports/Stock_Report_RC_YYYYMMDD_HHMMSS.csv

Format (sample):
    "Commodity","CutOffDate","PortId","LotsWithValCert","LotsNonTend","LotsSuspended"
    "RC","26-May-2026","AMS","0","0","0"
    "RC","26-May-2026","ANT","2114","1","0"
    ...
    "GrandTotal","","","3921","1","0"
"""
from __future__ import annotations

import csv
import io

from ._common import parse_ice_date, port_name


def parse_stock_report(text: str) -> dict:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return {"cut_off_date": None, "ports": [], "grand_total": {"with_val_cert": 0, "non_tend": 0, "suspended": 0}}

    ports: list[dict] = []
    grand = {"with_val_cert": 0, "non_tend": 0, "suspended": 0}
    cut_off_iso: str | None = None

    for r in rows[1:]:                                  # skip header row
        if len(r) < 6:
            continue
        commodity = r[0].strip()
        if commodity == "GrandTotal":
            grand = {
                "with_val_cert": int(r[3] or 0),
                "non_tend":      int(r[4] or 0),
                "suspended":     int(r[5] or 0),
            }
            continue
        if not cut_off_iso:
            cut_off_iso = parse_ice_date(r[1])
        port_id = r[2].strip()
        ports.append({
            "port_id":       port_id,
            "port_name":     port_name(port_id),
            "with_val_cert": int(r[3] or 0),
            "non_tend":      int(r[4] or 0),
            "suspended":     int(r[5] or 0),
        })

    return {
        "cut_off_date":   cut_off_iso,
        "ports":          sorted(ports, key=lambda p: -p["with_val_cert"]),
        "grand_total":    grand,
    }
