"""Implied per-origin outflow for ICE robusta certified stocks.

The C-contract / robusta source reports stock totals and an age-allowance
breakdown by `months_since_graded`, but never the per-origin mix of the
stock that's currently certified — that has to be deduced.

This module implements the cohort-DNA algorithm: every month_end ageing
report is a snapshot of how much of each cohort (= the calendar month
where the coffee was graded) is still on hand. Comparing successive
reports gives us per-cohort shrinkage at each port; apportioning that
shrinkage by the cohort's own origin split (its "DNA", extracted from
gradings events during the cohort's month) yields an implied per-origin
outflow that's mass-balanced.

Three pure builders below; the importer wires them into the robusta JSON.

──────────────────────────────────────────────────────────────────────
Notation
    cohort_id := "YYYY-MM" of the calendar month a lot was graded in.
    bucket    := one age-allowance row at (month_end ME, months_since k).
                 The cohort of that bucket is ME's month − k months.
    DNA       := normalised origin split of the gradings recorded for
                 a (port, cohort_id) pair. Falls back to the port's
                 all-time origin mix if no gradings events match the
                 cohort (older history is sometimes thinner than the
                 ageing report's coverage).
──────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from collections import defaultdict

# ── Month-ISO arithmetic ─────────────────────────────────────────────────────

def shift_month_iso(month_iso: str, k: int) -> str:
    """Return month_iso shifted by `k` months. `k` may be negative."""
    y, m = int(month_iso[:4]), int(month_iso[5:7])
    m += k
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def month_iso_of(date_iso: str) -> str:
    return date_iso[:7]


# ── Cohort DNA (per-port origin split per cohort month) ──────────────────────

def build_cohort_dna(
    gradings_per_port_month_origin: dict,
) -> dict:
    """Normalise raw lots-per-(port, cohort_iso, origin) into share weights.

    Input shape:  {port: {cohort_iso: {origin: lots}}}
    Output shape: {port: {cohort_iso: {origin: share ∈ [0, 1]}}}
    Cohort months with zero total are dropped.
    """
    out: dict[str, dict[str, dict[str, float]]] = {}
    for port, by_cohort in gradings_per_port_month_origin.items():
        port_out: dict[str, dict[str, float]] = {}
        for cohort, by_origin in by_cohort.items():
            total = sum(by_origin.values())
            if total <= 0:
                continue
            port_out[cohort] = {o: lots / total for o, lots in by_origin.items()}
        if port_out:
            out[port] = port_out
    return out


# ── Per-report cohort decomposition ──────────────────────────────────────────

def decompose_report_to_port_cohort_lots(report: dict) -> dict:
    """{port: {cohort_iso: lots}} from one ageing report.

    The report's `valid.buckets` carry `by_port` values in MT;
    we convert to lots (10 MT per lot) here.

    Both k=0 (current-month cohort, in sources that publish it) and
    k≥1 are accepted. Our actual ICE source emits k≥1 only, so the
    current-month cohort first surfaces in the *next* month's report
    at k=1.
    """
    me_iso = month_iso_of(report["month_end"])
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for bucket in report.get("valid", {}).get("buckets", []) or []:
        k = bucket.get("months_since_graded")
        if k is None or k < 0:
            continue
        cohort = shift_month_iso(me_iso, -k)
        for port, mt in (bucket.get("by_port") or {}).items():
            if mt <= 0:
                continue
            out[port][cohort] += mt / 10.0  # MT → lots
    return {p: dict(cmap) for p, cmap in out.items()}


# ── Implied outflow per (month_end, port, origin) ────────────────────────────

def build_implied_outflow(
    age_reports: list,
    cohort_dna: dict,
    port_alltime_dna: dict | None,
    gradings_per_port_month_origin: dict | None,
    *,
    dna_coverage_threshold: float = 0.5,
) -> list:
    """Walk consecutive ageing reports; apportion per-cohort shrinkage by DNA.

    For every pair (RP_{t-1}, RP_t), for every (port, cohort) seen in either:
      shrinkage = max(0, lots_in_RP_{t-1} − lots_in_RP_t)
      apportion by cohort DNA at this port → outflow per origin

    Two special handlers:
      • A cohort that just appeared in RP_t (its first bucket at k=1) didn't
        exist in RP_{t-1}. Its shrinkage = graded_lots[port][cohort] − bucket_k1.
        This captures the decert that happened between grading and the next
        month-end.
      • A cohort that exists in RP_{t-1} but not in RP_t (fully decertified)
        contributes its entire prev_lots as shrinkage.

    DNA fallback hierarchy:
        cohort_dna[port][cohort]  →  port_alltime_dna[port]  →  skipped.

    DNA coverage guard:
        If the raw gradings lots recorded for (port, cohort) cover less than
        `dna_coverage_threshold` of the cohort's first-appearance bucket size,
        the cohort DNA is treated as unreliable and the algorithm falls back
        to `port_alltime_dna`. This prevents a few stray gradings events
        from claiming the DNA of a cohort that's actually orders of
        magnitude larger. Threshold defaults to 0.5.

    Returns: [{"month_end": "YYYY-MM-DD",
               "by_port":         {port: {origin: lots}},   # total outflow
               "by_port_transit": {port: {origin: lots}}},  # "in & out" subset
              ...]
    one entry per RP_t (skipping the very first since there's no prior pair).

    `by_port_transit ⊆ by_port`: it is the outflow from cohorts graded inside
    this very interval (arrived AND left). Subtract it from `by_port` to get
    the legacy / pure outflow. The frontend uses it to split a window's
    gradings into pure "in" (stayed) vs "in & out" (graded then left).
    """
    if not age_reports:
        return []
    sorted_reps = sorted(age_reports, key=lambda r: r["month_end"])
    port_alltime_dna = port_alltime_dna or {}
    gradings_per_port_month_origin = gradings_per_port_month_origin or {}

    # Pre-pass: record the lots at which each (port, cohort) first appeared
    # in any report. Used for the DNA coverage guard.
    first_appearance: dict[str, dict[str, float]] = defaultdict(dict)
    for rep in sorted_reps:
        lots = decompose_report_to_port_cohort_lots(rep)
        for port, cmap in lots.items():
            for cohort, lc in cmap.items():
                if cohort not in first_appearance[port]:
                    first_appearance[port][cohort] = lc

    def resolve_dna(port: str, cohort: str) -> dict:
        """Cohort DNA if it covers enough of the cohort, else port alltime."""
        dna = cohort_dna.get(port, {}).get(cohort, {})
        if dna:
            graded_lots = sum(gradings_per_port_month_origin.get(port, {}).get(cohort, {}).values())
            first_seen = first_appearance.get(port, {}).get(cohort, 0.0)
            if first_seen > 0 and graded_lots < dna_coverage_threshold * first_seen:
                dna = {}      # gradings too thin to trust → fall through
        if not dna:
            dna = port_alltime_dna.get(port, {})
        return dna

    out: list[dict] = []
    for i in range(1, len(sorted_reps)):
        prev = sorted_reps[i - 1]
        cur = sorted_reps[i]
        cur_month_iso = month_iso_of(cur["month_end"])
        prev_month_iso = shift_month_iso(cur_month_iso, -1)

        prev_lots = decompose_report_to_port_cohort_lots(prev)
        cur_lots = decompose_report_to_port_cohort_lots(cur)

        all_ports = set(prev_lots.keys()) | set(cur_lots.keys())
        port_origin_out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        # Transit = outflow attributable to a cohort that was *graded inside
        # this same report interval* and already (partly) decertified by RP_t
        # — i.e. coffee that arrived AND left ("in & out"). It is the
        # new-cohort shrinkage only; everything else is legacy ("pure out").
        # `by_port_transit ⊆ by_port`, so pure_out = by_port − by_port_transit.
        port_origin_transit: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        # A "new cohort" first appears in RP_t — its `lp` is 0 because it
        # didn't exist in RP_{t-1}. The natural cohort_ids for this are
        # cur_month_iso (k=0 sources, e.g. the user's design example) and
        # prev_month_iso (k≥1 sources like our actual ICE feed).
        new_cohort_ids = {cur_month_iso, prev_month_iso}

        for port in all_ports:
            cohorts = set(prev_lots.get(port, {}).keys()) | set(cur_lots.get(port, {}).keys())
            # Force-include the newly-graded recent-month cohorts in case
            # they were fully decertified and never appeared in RP_t.
            cohorts.update(new_cohort_ids)

            for cohort in cohorts:
                lp = prev_lots.get(port, {}).get(cohort, 0.0)
                lc = cur_lots.get(port, {}).get(cohort, 0.0)
                is_transit = cohort in new_cohort_ids and lp == 0
                if is_transit:
                    graded_origins = gradings_per_port_month_origin.get(port, {}).get(cohort, {})
                    graded_total = sum(graded_origins.values())
                    if graded_total <= 0:
                        continue
                    shrink = max(0.0, graded_total - lc)
                else:
                    shrink = max(0.0, lp - lc)
                if shrink <= 0:
                    continue
                dna = resolve_dna(port, cohort)
                if not dna:
                    continue
                for origin, share in dna.items():
                    apportioned = shrink * share
                    if apportioned > 0:
                        port_origin_out[port][origin] += apportioned
                        if is_transit:
                            port_origin_transit[port][origin] += apportioned

        out.append({
            "month_end": cur["month_end"],
            "by_port": {p: {o: round(v, 2) for o, v in od.items() if v > 0}
                        for p, od in port_origin_out.items() if od},
            # In & out subset (graded AND decertified inside this interval).
            "by_port_transit": {p: {o: round(v, 2) for o, v in od.items() if v > 0}
                                for p, od in port_origin_transit.items() if od},
        })
    return out


# ── Current per-origin breakdown from the latest ageing report ───────────────

def build_current_by_origin(
    latest_report: dict,
    cohort_dna: dict,
    port_alltime_dna: dict | None,
    gradings_per_port_month_origin: dict | None = None,
    *,
    dna_coverage_threshold: float = 0.5,
) -> dict:
    """{port: {origin: lots}} from the latest ageing report.

    Per (port, bucket) lots are apportioned by the bucket's cohort DNA.
    Falls back to port_alltime_dna when:
        • the cohort has no DNA entry (predates our gradings history), OR
        • the raw gradings lots cover less than the threshold of the
          bucket size (gradings event data too thin to trust).
    """
    if not latest_report:
        return {}
    port_alltime_dna = port_alltime_dna or {}
    gradings_per_port_month_origin = gradings_per_port_month_origin or {}
    me_iso = month_iso_of(latest_report["month_end"])
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for bucket in latest_report.get("valid", {}).get("buckets", []) or []:
        k = bucket.get("months_since_graded")
        if k is None or k < 0:
            continue
        cohort = shift_month_iso(me_iso, -k)
        for port, mt in (bucket.get("by_port") or {}).items():
            if mt <= 0:
                continue
            lots = mt / 10.0
            dna = cohort_dna.get(port, {}).get(cohort, {})
            if dna:
                graded_lots = sum(gradings_per_port_month_origin.get(port, {}).get(cohort, {}).values())
                if graded_lots > 0 and lots > 0 and graded_lots < dna_coverage_threshold * lots:
                    dna = {}
            if not dna:
                dna = port_alltime_dna.get(port, {})
            if not dna:
                out[port]["?"] += lots
                continue
            for origin, share in dna.items():
                out[port][origin] += lots * share
    return {p: {o: round(v, 2) for o, v in od.items() if v > 0}
            for p, od in out.items() if od}


# ── Port-level all-time DNA (fallback for cohorts predating gradings data) ──

def build_port_alltime_dna(port_origin_history: dict) -> dict:
    """{port: {origin: share}} from the full-history tenderable-lots rollup."""
    out: dict[str, dict[str, float]] = {}
    for port, by_origin in (port_origin_history or {}).items():
        weights: dict[str, float] = {}
        total = 0.0
        for origin, stats in by_origin.items():
            t = stats.get("tenderable", 0) if isinstance(stats, dict) else float(stats)
            if t > 0:
                weights[origin] = float(t)
                total += float(t)
        if total > 0:
            out[port] = {o: w / total for o, w in weights.items()}
    return out
