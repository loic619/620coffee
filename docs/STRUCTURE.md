# Repository structure and conventions

## Grouping: by source, not by consumer

Datasets are grouped under `data/<source>/`, where `<source>` is the *upstream
publisher* — `ice`, `cftc`, `barchart`, `acaphe`. Grouping by source is what
makes this repository legible as a public catalogue: provenance, cadence,
attribution and terms are all properties of the source, so putting them in one
place lets a single per-directory README answer them for everything inside.

Consumers must not rely on directory layout. The mapping from a stable dataset
`id` to its current `path` lives in `catalog.json`; resolve through that. This is
what lets the layout be organised for readers while a consumer keeps whatever
local naming it already uses.

## Naming

- Directory names: lowercase, the source's short name, no punctuation.
- File names: lowercase `snake_case`, `.json`.
- Dataset ids: `<source>.<family>[.<variant>]`, e.g. `ice.certified_stocks.arabica`.
- Ids are permanent. A file may move; its id may not change or be reused.

## Splitting large series

A dataset that grows without bound is split into era files rather than rewritten
as one ever-larger blob, e.g. `..._2015-2019.json`, `..._2020-2024.json`. Each
era file is a separate catalogue entry with its own `coverage` range and hash, so
a consumer re-fetches only the era that changed — in practice only the current
one. Closed eras never change, which keeps both bandwidth and diff noise near
zero.

## What each directory is for

| Path | Contains | Must not contain |
|---|---|---|
| `data/` | Published datasets only | Anything not listed in `catalog.json` |
| `schemas/` | JSON Schema documents | Data |
| `docs/` | Provenance, contract, structure | Methodology or analysis |
| `scripts/` | Validation of what is already here | Collection, scraping or transformation of data |

`scripts/` is deliberately narrow. Tooling that *acquires* data is a publisher
concern and runs from the workflow that writes the data; tooling in this
repository only checks that what has been committed is well-formed.

## Adding a dataset

1. Write the file under `data/<source>/`, creating the directory and its
   `README.md` if the source is new.
2. Add an entry to `catalog.json` (see `docs/DATA_CONTRACT.md` for the fields).
3. Add or extend a schema in `schemas/` if the dataset introduces a new shape.
4. Record the source in `docs/SOURCES.md` if it is new.
5. Run `python3 scripts/validate_catalog.py` — CI runs the same check.

Data and its catalogue entry are committed **together**. A commit in which they
disagree is a broken publish, and validation fails it.
