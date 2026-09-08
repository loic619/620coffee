# 620coffee

**A public, redistributable catalogue of coffee market datasets.**

620coffee publishes tidied, versioned snapshots of publicly sourced coffee market
and fundamentals data — exchange stock reports, regulatory positioning reports,
price and options series, and physical market quotes. Each dataset is a plain
JSON file with a stable path, described by a machine-readable catalogue.

It is a *data* repository. It contains no analytics, no models, no signals and no
application code — only the data and the small amount of tooling needed to
validate and describe it.

## Quick start

Every dataset is reachable over plain HTTPS, no authentication and no API client:

```bash
# 1. Read the catalogue (small, changes on every publish)
curl -sL https://raw.githubusercontent.com/loic619/620coffee/main/catalog.json

# 2. Fetch a dataset named in it
curl -sL https://raw.githubusercontent.com/loic619/620coffee/main/data/ice/<file>.json
```

`catalog.json` carries a `sha256` and `updated_at` for every dataset, so a
consumer can fetch the catalogue alone and pull only the files that actually
changed. See [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) before you build
against it.

## What is in here

| Directory | Source | Contents |
|---|---|---|
| `data/ice/` | ICE (Intercontinental Exchange) | Certified stocks reports, including deep history |
| `data/cftc/` | CFTC | Commitments of Traders positioning reports |
| `data/barchart/` | Barchart | Futures price series and options data |
| `data/acaphe/` | Acaphe | Vietnamese physical coffee quotes |

The per-source directories carry their own `README.md` describing upstream,
cadence and attribution. The full index is [`CATALOG.md`](CATALOG.md) for humans
and [`catalog.json`](catalog.json) for machines.

> **Status:** the structure, catalogue format and data contract are in place; the
> datasets themselves are being migrated in and the source directories are empty
> until that lands. `catalog.json` is the authority on what is actually
> published at any commit.

## What is deliberately *not* in here

- Derived analytics, indicators, forecasts or trading signals
- Research, methodology or modelling work
- Private or non-redistributable datasets
- Application, frontend or infrastructure code
- Any credential, token or private endpoint

These live elsewhere and are out of scope for this repository by design. This
repository has no dependency on any downstream consumer and stands alone.

## Terms and attribution

Datasets are redistributed from publicly available sources. Per-source
provenance, upstream links and attribution requirements are recorded in
[`docs/SOURCES.md`](docs/SOURCES.md) and in each source directory's README.

Data is published as-is, on a best-effort basis, with no warranty of accuracy,
completeness or availability. It is not investment advice. Where an upstream
source is authoritative, it — not this repository — is the citation of record.

## Layout

```
catalog.json          machine-readable index of every published dataset
CATALOG.md            the same index, human-readable
data/<source>/        datasets, grouped by upstream source
schemas/              JSON Schema for the catalogue and for dataset families
docs/                 structure, data contract and source provenance
scripts/              validation tooling (no data collection)
```

See [`docs/STRUCTURE.md`](docs/STRUCTURE.md) for the conventions these follow.
