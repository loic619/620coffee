# Porting note — the fetch code in this directory

Everything under `fetch/scraper/` is 619's ICE certified-stocks scraper, moved
here so it runs on free public-repository Actions instead of metered private
ones. The fetch is a deliberately rate-limit-paced sweep at 4 s/request: roughly
42 minutes per run, almost all of it waiting. Waiting is what became free.

## The port is byte-identical, on purpose

Every parser — `fetch.py`, `spa_api.py`, `parse_*.py`, `_common.py`,
`cohort_outflow.py`, `ice_arabica_groups.py`, `run_degradations.py` — is a
verbatim copy. The package layout (`scraper/sources/ice_certified_stocks/`) is
reproduced exactly so the relative imports (`from ... import run_degradations`,
`from ..ice_arabica_groups import group_of`) resolve without edits.

That is deliberate: while both repositories run this code in parallel, a diff
between their outputs has to mean *the fetch differs*, not *the code differs*.

**`orchestrate.py` is the only file that differs, in exactly two places**, both
marked `PORTED TO 620`:

| What | Upstream (619) | Here | Why |
|---|---|---|---|
| `OUT_DIR` | `frontend/public/data` | `_stage/`, via `ICE_STAGE_DIR` | 620 has no frontend. Output is staged, then filtered by `publish_ice.py`. |
| `BLOCK_STATE_PATH` | repo-root `data/` | `fetch/state/` | Repo-root `data/` here is the published payload directory. |

Pacing and the sweep window are **no longer divergences**: 619 adopted the same
4.0 / 4.0 / 8.0 baseline and the same second-level window, so those constants are
identical in both repos. `tests/test_pacing_baseline.py` pins them anyway,
because they are load-bearing and non-obvious.

### Fetch parity with 619

Everything on the fetch path is byte-identical apart from the two anchors above:
all ten parsers, `fetch.py`, `spa_api.py`, `cohort_outflow.py`,
`ice_arabica_groups.py`, `run_degradations.py`, and the whole of
`orchestrate.py` including the tier logic, the sweep window, the 403/429
breakers, section boundaries, retry ladder, headers and session handling.

Verified 2026-09-08 after 619 moved to a second-level sweep window. 620 was
still walking whole minutes — 1,920 candidates over 10:29:00–11:00:59 against
619's 1,810 over 10:29:50–10:59:59 — which is exactly the kind of silent drift
this note exists to catch. Re-verify whenever 619's ICE code changes; the diff
is the audit.

### Tier-1 publish-time state

`stock_report_hits.json` is committed here, exactly as it is in 619, and holds
the same dated history: one entry per date, `{"date", "hhmmss"}`. It was
bootstrapped from 619's copy so 620 starts with the same tier-0 and tier-1
knowledge rather than relearning from zero, and the fetch workflow commits it
back after every run.

There is **no secret**. An earlier design injected an undated
`ICE_TIER1_HINTS` repository secret because the date-to-second mapping was
assumed to be private; it is not. ICE publishes these reports publicly and the
log records only when it did so. That design has been removed: it gave 620 a
different state model from 619, left tier 0 dead here, and made the two repos
search in different orders — which would have quietly hollowed out the shadow
comparison it was supposed to be validated by.

Consequences, stated plainly:

- **Tier 0 works.** `_recorded_time_for()` resolves a date that either repo has
  already captured, so a known day is one GET rather than a search.
- **Tier 1 works and keeps learning.** Ranking is derived from this file by the
  same top-K frequency computation, so 619 and 620 produce the same candidate
  list from the same history.
- **No secret is required** to run the fetch, and nothing about tier 0 or
  tier 1 depends on repository configuration.

`tests/test_stock_report_hits_state.py` pins every one of those semantics, and
its twin in 619 (`backend/scraper/tests/test_stock_report_hits_state.py`) is
byte-identical below its PARITY BODY marker.

```sh
diff <path-to-619>/backend/scraper/sources/ice_certified_stocks/orchestrate.py \
     fetch/scraper/sources/ice_certified_stocks/orchestrate.py
```

Anything beyond those two hunks is drift and should be reconciled.

## What was deliberately *not* ported

| File | Why it stayed in 619 |
|---|---|
| `news_emit.py` | Writes engine commentary into the news feed. Not acquisition. |
| `record_observation.py`, `build_run_history.py` | Operator tooling and a 619-side run ledger. Not acquisition; the fetcher already records its own captures into `stock_report_hits.json`. |
| `probe_*.py` | Diagnostics; they cost no measurable Actions time. |

`cohort_outflow.py` *is* here only because `orchestrate.py` imports it. Its
output is derived, so the allow-list strips it before publication and 619
recomputes it after the merge.

## What this code is allowed to publish

It runs with `merge=False`, so the result holds only the days the run fetched.
`publish_ice.py` then:

1. prunes to `fetch/allowlist.py` — an allow-list, so an unanticipated field
   cannot leak by being unlisted;
2. trims any dated record older than the window — the orchestrator's
   `recent_activity` and `monthly` structures accumulate, and a three-snapshot
   run was measured carrying 152 tender records reaching back fifteen months;
3. refuses outright to write a payload of more than 60 snapshots.

`scripts/check_allowlist.py` then re-checks what is on disk, independently of
the code that wrote it, in CI and before every commit.

## What fetch state is committed, and what is not

**Committed:** `stock_report_hits.json`. It is learned state — see above — and
must survive across runs or 620 would search differently from 619 on every
fetch.

**Not committed:** `stock_report_cursor.json`, `ice_run_stats.json` and
`fetch/state/` — see `.gitignore`. These are per-run: where an interrupted
sweep got to, which dates tier 1 has already been spent on, request/throttle
diagnostics, and block-notification edge state. None of them carry forward.
Run stats go up as a 30-day workflow artifact instead, which is enough to
compare 620's request economics against 619's without a churn commit per fetch.

## Keeping the two copies from drifting

While 619 still fetches ICE, both copies run. That is temporary and intended to
end: once the shadow comparison passes, 619's fetch path is disabled and this
becomes the only copy. 619 keeps the merge and every derivation
(`_merge_arabica`, `_merge_robusta`, `build_port_peaks`,
`recompute_robusta_cohort_outflow`) — those were never ported and must not be.
