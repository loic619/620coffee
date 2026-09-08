"""Raw-field allow-lists for what 620 may publish.

620 is an acquisition worker. It runs 619's parsers unmodified, which means the
object it produces in memory contains engine-derived fields alongside the raw
report values. Those derived fields must never reach a public file.

The rule is an ALLOW-list, never a deny-list: a key that is not named here is
dropped. A new field appearing upstream — in an ICE report or in 619's
derivation — therefore cannot leak by being unanticipated. It simply does not
get published until someone adds it here deliberately.

Every exclusion below is annotated with why it is not raw.
"""

from __future__ import annotations

# ── ICE certified stocks — arabica ───────────────────────────────────────────
# Excluded from the payload:
#   port_peaks  — an engine-maintained ratchet (build_port_peaks) that only ever
#                 widens; it is a derived statistic over accumulated history,
#                 not anything ICE publishes.
#   errors      — per-source fetch diagnostics; they belong in the .status
#                 sidecar, where a consumer looks for them, not in the data.
ICE_ARABICA = {
    "generated_at": True,
    "as_of": True,
    "source_url": True,
    "snapshots": {
        "date": True,
        "report_date": True,
        "total_bags": True,
        "transition_bags": True,
        "pending_grading_bags": True,
        "rebagging_bags": True,
        "passed_today_bags": True,
        "failed_today_bags": True,
        "by_port": True,
        "by_group": True,
        "sections": True,
        "issuers_today": True,
        "stoppers_today": True,
        "issued_total_today": True,
        "received_total_today": True,
        "graded_today_by_origin": True,
        "graded_today_by_port_origin": True,
        "passed_by_origin": True,
        # Parse-step output (orchestrate.py:1275), and read by the Demand and
        # Research panels. Raw report detail, the counterpart of
        # passed_by_origin — not a derivation.
        "failed_by_origin": True,
    },
    "latest_detail": {
        "report_date": True,
        "total_certified": True,
        "transition": True,
        "pending_grading": True,
        "rebagging": True,
        "grading_today": True,
        "age_detail": True,
        "age_detail_date": True,
    },
    "ageing_report": True,      # verbatim parse of ICE's published ageing report
    "ageing_report_url": True,
}

# ── ICE certified stocks — robusta ───────────────────────────────────────────
# Excluded from the payload:
#   port_peaks           — same engine ratchet as arabica.
#   port_origin_history  — accumulated per-port history. History lives in 619;
#                          620 publishes only the freshly fetched window.
#   monthly.implied_outflow    — cohort_outflow.py output. Derived.
#   monthly.current_by_origin  — cohort-DNA derivation. Derived.
ICE_ROBUSTA = {
    "generated_at": True,
    "as_of": True,
    "daily_fetched": True,
    "snapshots": {
        "date": True,
        "cut_off_date": True,
        "total_lots_certified": True,
        "non_tend_lots": True,
        "suspended_lots": True,
        "lots_graded_today": True,
        "lots_sold_today": True,
        "lots_bought_today": True,
        "tenders_today": True,
        "by_port_lots": True,
        # round(total_lots * 10_000 / 60) — a unit conversion of a reported
        # figure, computed in the parse step (orchestrate.py:1295) and read by
        # the frontend and the Telegram brief. Arithmetic on a raw number is
        # not methodology; dropping it would blank those panels.
        "total_bags_60kg_equivalent": True,
    },
    "latest_detail": {
        "stock_report": True,
        "stock_report_url": True,
    },
    "recent_activity": {          # verbatim parses of ICE's daily reports
        "gradings": True,
        "grading_appeals": True,
        "iss_recv_daily": True,
        "tenders": True,
        "grading_overview": True,
        "infested_warrants": True,
    },
    "monthly": {
        "iss_recv_monthly": True,   # ICE's published monthly issuer/receiver report
        "age_allowance": True,      # ICE's published age-allowance report
    },
}

SPECS = {
    "certified_stocks_arabica": ICE_ARABICA,
    "certified_stocks_robusta": ICE_ROBUSTA,
}


def prune(value, spec, path=""):
    """Return `value` keeping only what `spec` allows; collect what was dropped.

    Returns (kept, dropped_paths). A spec of True means "keep this subtree
    verbatim" — used where the whole structure is a report parse. A dict spec
    recurses. Lists map the spec over their items.
    """
    dropped: list[str] = []

    if spec is True:
        return value, dropped

    if isinstance(spec, dict) and isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            sub = spec.get(key)
            if sub is None:
                dropped.append(f"{path}{key}")
                continue
            kept_item, sub_dropped = prune(item, sub, f"{path}{key}.")
            kept[key] = kept_item
            dropped.extend(sub_dropped)
        return kept, dropped

    if isinstance(value, list):
        kept_list = []
        for index, item in enumerate(value):
            kept_item, sub_dropped = prune(item, spec, f"{path}[{index}].")
            kept_list.append(kept_item)
            # Report each distinct dropped field once, not once per row.
            for d in sub_dropped:
                generic = f"{path}[].{d.split('].', 1)[-1]}" if "]." in d else d
                if generic not in dropped:
                    dropped.append(generic)
        return kept_list, dropped

    # Spec expects structure, value is a scalar (or vice versa): keep nothing.
    return None, [path.rstrip(".")]


def allowed_key_paths(spec, path="") -> set[str]:
    """Every key path the spec permits — used by the CI conformance check."""
    if spec is True:
        return {path.rstrip(".")} if path else set()
    out: set[str] = set()
    for key, sub in spec.items():
        here = f"{path}{key}"
        out.add(here)
        if isinstance(sub, dict):
            out |= allowed_key_paths(sub, f"{here}.")
    return out
