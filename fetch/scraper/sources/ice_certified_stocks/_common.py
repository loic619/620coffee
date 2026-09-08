"""Shared helpers: date parsing, port-code lookup."""
from __future__ import annotations

from datetime import datetime

# Robusta certified-warehouse port codes (LIFFE convention). Codes confirmed
# from the probe's stock_report.csv sample. Where a code is uncertain the
# `port_name` lookup returns None and the JSON keeps the bare code.
ROBUSTA_PORTS: dict[str, str] = {
    "AMS": "Amsterdam",
    "ANT": "Antwerp",
    "BAR": "Barcelona",
    "BRE": "Bremen",
    "FEL": "Felixstowe",
    "HAM": "Hamburg",
    "LEH": "Le Havre",
    "LIV": "Liverpool",
    "LON": "London",
    "NOR": None,            # uncertain — kept as code
    "ROT": "Rotterdam",
    "TRI": "Trieste",
}


def port_name(code: str) -> str | None:
    return ROBUSTA_PORTS.get((code or "").strip().upper())


def parse_ice_date(s: str) -> str | None:
    """'21-May-2026' → '2026-05-21'. Returns None if unparseable."""
    try:
        return datetime.strptime((s or "").strip(), "%d-%b-%Y").date().isoformat()
    except Exception:
        return None


def parse_ice_month(s: str) -> str | None:
    """'Mar-2026' → '2026-03'. Returns None if unparseable."""
    try:
        return datetime.strptime((s or "").strip(), "%b-%Y").strftime("%Y-%m")
    except Exception:
        return None
