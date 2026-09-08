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

**`orchestrate.py` is the only file that differs, in exactly four places** — two
path anchors marked `PORTED TO 620`, and two pacing constants marked
`PACING BASELINE`:

| What | Upstream (619) | Here | Why |
|---|---|---|---|
| `OUT_DIR` | `frontend/public/data` | `_stage/`, via `ICE_STAGE_DIR` | 620 has no frontend. Output is staged, then filtered by `publish_ice.py`. |
| `BLOCK_STATE_PATH` | repo-root `data/` | `fetch/state/` | Repo-root `data/` here is the published payload directory. |
| `_THROTTLE` | `{"public": 2.0, "marketdata": 5.0}` | `{"public": 4.0, "marketdata": 8.0}` | 619's values are refused outright on the public runner pool. |
| `_STOCK_SWEEP_INTERVAL_S` | `3.0` | `4.0` | Same reason. |

### The pacing baseline is load-bearing

ICE answers GitHub's **public**-repository runner pool with `403` at 619's
pacing, from the first request of every section — a WAF page, not the file
server. The same code on 619's private pool the same day: 1,191 requests,
12 × 200, 0 × 403. Slowing the requests cleared it, and the run then progressed
into the expected 404-heavy timestamp search.

Three intervals, deliberately separate, and **not to be collapsed into one
global throttle**:

```
sweep timestamp probing     4.0s
publicdocs / US reports     4.0s
marketdata / LIFFE          8.0s
```

That `/marketdata/` needs the slower interval is observed behaviour, not a
guess. `tests/test_pacing_baseline.py` pins all of it, including that the
families stay distinct. Do not revert these to 619's values to "reduce the
diff" — that restores the 403.

Verify with:

```sh
diff <path-to-619>/backend/scraper/sources/ice_certified_stocks/orchestrate.py \
     fetch/scraper/sources/ice_certified_stocks/orchestrate.py
```

Anything beyond those two hunks is drift and should be reconciled.

## What was deliberately *not* ported

| File | Why it stayed in 619 |
|---|---|
| `news_emit.py` | Writes engine commentary into the news feed. Not acquisition. |
| `record_observation.py`, `build_run_history.py` | Record observed ICE publish times — a timing edge that stays private. |
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

## Fetch state is not committed

`stock_report_hits.json`, `stock_report_cursor.json`, `ice_run_stats.json` and
`fetch/state/` are git-ignored — see `.gitignore`, which explains why. The
short version: the hits file records the exact second ICE publishes each report.
That is an observed timing edge and it stays private. It exists only to let a
run skip ahead in the sweep, saving billed minutes — which is exactly the cost
620 does not pay.

## Keeping the two copies from drifting

While 619 still fetches ICE, both copies run. That is temporary and intended to
end: once the shadow comparison passes, 619's fetch path is disabled and this
becomes the only copy. 619 keeps the merge and every derivation
(`_merge_arabica`, `_merge_robusta`, `build_port_peaks`,
`recompute_robusta_cohort_outflow`) — those were never ported and must not be.
