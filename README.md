# 620coffee

**A public acquisition worker.** It fetches published market reports on a
schedule, parses them, and publishes the observations it just collected.

That is the whole job. 620coffee is not an archive and not a historical
database: each dataset here holds **only the most recent window it fetched** —
typically the last few business days. Older observations are not kept, and no
history is reconstructed here.

## Why it exists

GitHub Actions are free on public repositories. Some data collection is slow
without being computationally expensive — the ICE certified-stock sweep is
rate-limit paced at four seconds a request, so a run takes about forty minutes
of almost pure waiting. Running that work here, in the open, costs nothing.

Publishing the result is a consequence of running in public, not the goal.

## What is published

| Dataset | Source | Contents |
|---|---|---|
| `data/ice/certified_stocks_arabica_latest.json` | ICE | The days most recently fetched from ICE's published certified-stock reports |
| `data/ice/certified_stocks_robusta_latest.json` | ICE | As above, for robusta |

Each payload has a `.status.json` sidecar carrying `ok`, `fetched_at`, `sha256`,
the window covered, and any per-source fetch errors. **Read the status first**:
check `ok`, then verify the payload's bytes against its `sha256`.

`catalog.json` indexes what is currently published. See
[`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) before building on any of it.

```bash
curl -sL https://raw.githubusercontent.com/loic619/620coffee/main/data/ice/certified_stocks_arabica_latest.status.json
curl -sL https://raw.githubusercontent.com/loic619/620coffee/main/data/ice/certified_stocks_arabica_latest.json
```

Note that `raw.githubusercontent.com` caches for five minutes (measured: an
updated file became visible 292 seconds after the push), and a query-string
cache-buster does not bypass it.

## What is *not* here, and will not be

- **History.** No archives, no long series, no backfills. A payload is a window.
  Anything that accumulates observations does so elsewhere.
- **Derived values.** No indicators, models, signals, rankings or analytics. The
  fetch code produces some derived fields in passing; an allow-list
  (`fetch/allowlist.py`) strips them before anything is written, and
  `scripts/check_allowlist.py` re-checks the files independently in CI.
- **Credentials.** Nothing here needs one, to read or to run. The fetch targets
  public reports.

## Layout

```
data/<source>/        current-window payloads and their .status.json sidecars
fetch/                the acquisition worker — see fetch/PORTING.md
  allowlist.py        the only fields that may be published
  publish_ice.py      fetch → prune → trim to window → write
catalog.json          index of what is published right now
scripts/              validation: catalogue, allow-list, credential shapes
docs/                 the data contract and source provenance
```

## Licence

| What | Licence |
|---|---|
| `data/`, `catalog.json`, documentation | [CC0 1.0](LICENSE) — public domain dedication |
| `fetch/`, `scripts/` | [MIT](scripts/LICENSE) |

Data is published as-is, on a best-effort basis, with no warranty of accuracy,
completeness or availability, and is not investment advice. Upstream sources are
recorded in [`docs/SOURCES.md`](docs/SOURCES.md) and are the citation of record.
