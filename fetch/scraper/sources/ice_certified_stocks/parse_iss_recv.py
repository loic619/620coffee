"""Issuers/Receivers reports — daily (with origin breakdown) and monthly (totals).

Source URLs:
  …/daily_issuers_receivers/irrrc_YYMMDD.txt
  …/monthly_issuers_receivers/irrrc_mYYMMDD.txt

Daily format: per-member section with "Tenders for : XYZ" then per-origin rows
("OriginName  sold  bought") then "Total For Member  sold  bought", finally
"Total  sold  bought" grand total.

Monthly format: flat list of "MEMBER  sold  bought" then "Total  sold  bought".
"""
from __future__ import annotations

import re

from ._common import parse_ice_date, parse_ice_month

_REPORT_DATE = re.compile(r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+\d{2}:\d{2}")
_DELIVERY_DATE = re.compile(r"Delivery\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{4})")
_DELIVERY_MONTH = re.compile(r"Delivery\s*:\s*([A-Za-z]{3}-\d{4})")

_MEMBER_HEADER = re.compile(r"Tenders\s+for\s*:\s*([A-Z]+)")
_ROW = re.compile(r"^(?P<label>[A-Za-z][\w '\-/]+?)\s{2,}(?P<sold>\d+)\s+(?P<bought>\d+)\s*$")


# ── Daily ────────────────────────────────────────────────────────────────────

def parse_iss_recv_daily(text: str) -> dict:
    report_date: str | None = None
    delivery_date: str | None = None
    members: list[dict] = []
    grand_total = {"sold": 0, "bought": 0}
    cur: dict | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if report_date is None:
            m = _REPORT_DATE.search(line)
            if m:
                report_date = parse_ice_date(m.group(1))
        if delivery_date is None:
            m = _DELIVERY_DATE.search(line)
            if m:
                delivery_date = parse_ice_date(m.group(1))
        m = _MEMBER_HEADER.search(line)
        if m:
            cur = {"code": m.group(1), "rows": [], "total_sold": 0, "total_bought": 0}
            members.append(cur)
            continue
        m = _ROW.match(line.strip())
        if not m:
            continue
        label = m.group("label").strip()
        sold = int(m.group("sold")); bought = int(m.group("bought"))
        if label.lower() == "total":
            grand_total = {"sold": sold, "bought": bought}
            cur = None
        elif label.lower().startswith("total for member"):
            if cur is not None:
                cur["total_sold"] = sold; cur["total_bought"] = bought
                cur = None
        elif cur is not None:
            cur["rows"].append({"origin": label, "sold": sold, "bought": bought})

    return {
        "report_date":   report_date,
        "delivery_date": delivery_date,
        "members":       members,
        "grand_total":   grand_total,
    }


# ── Monthly ──────────────────────────────────────────────────────────────────

def parse_iss_recv_monthly(text: str) -> dict:
    report_date: str | None = None
    month_iso: str | None = None
    members: list[dict] = []
    grand_total = {"sold": 0, "bought": 0}

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if report_date is None:
            m = _REPORT_DATE.search(line)
            if m:
                report_date = parse_ice_date(m.group(1))
        if month_iso is None:
            m = _DELIVERY_MONTH.search(line)
            if m:
                month_iso = parse_ice_month(m.group(1))
        m = _ROW.match(line.strip())
        if not m:
            continue
        label = m.group("label").strip()
        sold = int(m.group("sold")); bought = int(m.group("bought"))
        if label.lower() == "total":
            grand_total = {"sold": sold, "bought": bought}
        else:
            members.append({"code": label, "sold": sold, "bought": bought})

    return {
        "report_date":  report_date,
        "month":        month_iso,
        "members":      members,
        "grand_total":  grand_total,
    }
