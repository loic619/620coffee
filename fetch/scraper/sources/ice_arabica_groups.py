"""ICE Coffee "C" (Arabica / KC) origin groupings & per-group premium.

ICE classifies each deliverable arabica origin into one of five groups for the
C-contract certification & pricing. Group determines the per-pound premium or
discount applied vs the front-month settlement at delivery. Source: ICE Coffee
"C" contract specifications.

Used by the certified-stocks scraper to bucket each origin row from the daily
coffee_cert_stock_YYYYMMDD.xls report so the JSON can roll up by_port → by_group
→ by_origin (matching the drill-down UI specified in the certified-stocks view).
"""
from __future__ import annotations

# Per-group premium (positive) / discount (negative) in US cents per pound,
# applied vs the C-contract front-month settlement at delivery.
GROUP_PREMIUM_CENTS_LB: dict[str, int] = {
    "Group 0":    0,
    "Group 1":  600,   # Colombian milds
    "Group 2": -100,
    "Group 3": -400,
    "Group 4": -600,
}

# Canonical ICE-recognised arabica origins → group.  Origin labels here match
# (case-insensitive) what the ICE daily XLS uses; the parser normalises by
# stripping whitespace and applying _NAME_ALIASES below before lookup.
ORIGIN_GROUPS: dict[str, str] = {
    "Brazil":             "Group 4",
    "Burundi":            "Group 2",
    "Colombia":           "Group 1",
    "Costa Rica":         "Group 0",
    "Dominican Republic": "Group 3",
    "Ecuador":            "Group 3",
    "El Salvador":        "Group 0",
    "Guatemala":          "Group 0",
    "Honduras":           "Group 0",
    "India":              "Group 2",
    "Kenya":              "Group 0",
    "Mexico":             "Group 0",
    "Nicaragua":          "Group 0",
    "Panama":             "Group 0",
    "Papua New Guinea":   "Group 0",
    "Peru":               "Group 0",
    "Rwanda":             "Group 2",
    "Tanzania":           "Group 0",
    "Uganda":             "Group 0",
    "Venezuela":          "Group 2",
    "Vietnam":            "Group 4",
}

# Tolerant lookup: ICE files sometimes use abbreviated or alternate forms.
_NAME_ALIASES: dict[str, str] = {
    "salvador":            "El Salvador",
    "dominican rep":       "Dominican Republic",
    "dominican republic":  "Dominican Republic",
    "papua new-guinea":    "Papua New Guinea",
    "png":                 "Papua New Guinea",
    "viet nam":            "Vietnam",
}


def normalise_origin(raw: str) -> str:
    """Lowercase + trim + alias-map an origin label from the source file."""
    s = (raw or "").strip()
    key = " ".join(s.lower().split())
    if key in _NAME_ALIASES:
        return _NAME_ALIASES[key]
    # Case-insensitive match against canonical names.
    for canonical in ORIGIN_GROUPS:
        if canonical.lower() == key:
            return canonical
    return s  # unknown — return as-is; caller bucket as "Unknown"


def group_of(origin: str) -> str | None:
    """ICE C-contract group ('Group 0'..'Group 4') for an origin, or None."""
    return ORIGIN_GROUPS.get(normalise_origin(origin))


def premium_of(origin: str) -> int | None:
    """Per-lb premium (positive) / discount (negative) in US cents, or None."""
    grp = group_of(origin)
    return GROUP_PREMIUM_CENTS_LB.get(grp) if grp else None


def origins_in_group(group: str) -> list[str]:
    """All canonical origin names assigned to the given group."""
    return sorted(o for o, g in ORIGIN_GROUPS.items() if g == group)
