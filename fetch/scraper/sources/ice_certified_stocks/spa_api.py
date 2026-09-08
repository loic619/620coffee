"""ICE Market Data SPA endpoint — thin alternative to the XLS scrape path.

The marketdata.theice.com report page is an SPA whose data lives behind a
POST to /marketdata/api/reports/142/data with a JSON body. Used as a quick
freshness probe and a robust fallback when the heavier XLS pipeline in
orchestrate.py hits a structural change.

Endpoint:
    POST https://www.theice.com/marketdata/api/reports/142/data
Body:
    {"exchangeCodeAndContract": "IFUS,KC"}     # Arabica
    {"exchangeCodeAndContract": "IEU,RC"}      # Robusta

Returns a normalised dict:
    {
      "market":      "arabica" | "robusta",
      "contract":    "KC" | "RC",
      "as_of":       "YYYY-MM-DD" | None,
      "total":       int,                  # native units (bags or lots)
      "unit":        "bags" | "lots",
      "by_warehouse": {name: int, ...},    # warehouse breakdown
      "raw":         <the parsed JSON> ,
    }

Sandbox networking is restricted, so live calls happen in CI. The shape
above is validated by fixture-driven tests; defensive defaults let the
parser tolerate ICE adding fields without breaking us.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import requests

API_URL = "https://www.theice.com/marketdata/api/reports/142/data"
DEFAULT_HEADERS = {
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent":   ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
}

MARKETS = {
    "arabica": {"contract": "KC", "exchange_code": "IFUS,KC", "unit": "bags"},
    "robusta": {"contract": "RC", "exchange_code": "IEU,RC",  "unit": "lots"},
}


def _parse_as_of(payload: dict[str, Any]) -> str | None:
    """Pull an as-of/report date out of whichever field ICE provides.
    Tolerant: accepts ISO date, 'DD-MMM-YYYY', or 'YYYY-MM-DDTHH:MM:SSZ'."""
    for k in ("reportDate", "asOfDate", "asOf", "date", "selectedDate"):
        v = payload.get(k)
        if not v or not isinstance(v, str):
            continue
        s = v.strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                    "%d-%b-%Y", "%d-%b-%y"):
            try:
                return datetime.strptime(s[:len(fmt) + 4], fmt).date().isoformat()
            except ValueError:
                continue
        # Fall back to ISO prefix slicing — accept "2026-05-29T..." → "2026-05-29".
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
    return None


def _parse_warehouse_rows(payload: dict[str, Any]) -> tuple[dict[str, int], int]:
    """Find the list of warehouse rows and return ({name: value}, total).

    Tolerant of three shapes ICE has been known to use:
      payload['rows']     = [{'warehouseName': 'Antwerp', 'value': 123}, ...]
      payload['data']     = [{'name': 'Antwerp', 'total': 123}, ...]
      payload['warehouses'] = [{'location': 'Antwerp', 'bags': 123}, ...]
    """
    rows: list[dict] | None = None
    for k in ("rows", "data", "warehouses", "records"):
        v = payload.get(k)
        if isinstance(v, list) and v:
            rows = v
            break
    if not rows:
        return {}, int(payload.get("total") or payload.get("grandTotal") or 0)

    name_keys  = ("warehouseName", "warehouse", "location", "name", "port")
    value_keys = ("value", "total", "bags", "lots", "quantity")
    by: dict[str, int] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = next((str(r[k]).strip() for k in name_keys if r.get(k)), None)
        val  = next((r[k] for k in value_keys if r.get(k) is not None), None)
        if name is None or val is None:
            continue
        try:
            by[name] = by.get(name, 0) + int(val)
        except (TypeError, ValueError):
            continue
    total = sum(by.values()) if by else int(payload.get("total") or payload.get("grandTotal") or 0)
    return by, total


def parse_spa_response(market: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Turn the ICE SPA JSON into the normalised shape the rest of the
    pipeline consumes. Pure function — no I/O — for fixture-based testing."""
    if market not in MARKETS:
        raise ValueError(f"unknown market: {market}")
    cfg = MARKETS[market]
    by, total = _parse_warehouse_rows(payload)
    return {
        "market":       market,
        "contract":     cfg["contract"],
        "as_of":        _parse_as_of(payload),
        "total":        total,
        "unit":         cfg["unit"],
        "by_warehouse": by,
        "raw":          payload,
    }


def fetch_spa(market: str, selected_date: str | None = None,
              timeout: float = 20.0, session: requests.Session | None = None,
              ) -> dict[str, Any]:
    """POST to the ICE marketdata SPA and return the normalised dict.

    Raises requests.HTTPError on non-200, ValueError on non-JSON. Sandbox
    egress is blocked; CI is the live test bed.
    """
    if market not in MARKETS:
        raise ValueError(f"unknown market: {market}")
    body: dict[str, Any] = {"exchangeCodeAndContract": MARKETS[market]["exchange_code"]}
    if selected_date:
        body["selectedDate"] = selected_date
    s = session or requests.Session()
    r = s.post(API_URL, headers=DEFAULT_HEADERS,
               data=json.dumps(body), timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict):
        # Some ICE reports wrap the row list at top level; coerce so the
        # downstream shape is consistent.
        payload = {"rows": payload}
    return parse_spa_response(market, payload)
