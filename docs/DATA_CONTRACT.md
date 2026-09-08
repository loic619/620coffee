# Data contract

What a consumer of this repository can rely on, and what it must not.

## The catalogue is the interface

`catalog.json` at the repository root is the single authority on what is
published. Anything under `data/` that is not listed there is not published —
treat it as absent. Do not discover datasets by listing directories.

```jsonc
{
  "catalog_version": 1,          // bumped only on a breaking change to this shape
  "generated_at": "2026-09-08T00:00:00Z",
  "datasets": [
    {
      "id": "ice.certified_stocks.arabica",   // permanent, never reused
      "title": "ICE certified arabica stocks",
      "source": "ice",                        // matches data/<source>/ and docs/SOURCES.md
      "path": "data/ice/certified_stocks_arabica.json",
      "format": "json",
      "bytes": 2686578,
      "sha256": "…",                          // of the file's exact bytes
      "updated_at": "2026-09-08T00:00:00Z",   // when the content last changed
      "cadence": "daily",                     // expected publish rhythm
      "coverage": { "start": "1990-01-01", "end": "2026-09-05" },
      "schema": "schemas/ice.certified_stocks.schema.json",  // optional
      "deprecated": false
    }
  ]
}
```

## Guarantees

- **Stable ids.** An `id` always refers to the same logical dataset. Paths may
  move; resolve `id` → `path` through the catalogue on every fetch.
- **Atomic publish.** A dataset file and its catalogue entry change in the same
  commit. Any commit on `main` is internally consistent — the `sha256` in the
  catalogue matches the file beside it.
- **Honest hashes.** `sha256` and `bytes` describe the exact bytes served by
  `raw.githubusercontent.com` for that commit. They are the supported way to
  decide whether a re-fetch is needed.
- **Additive schema change.** New fields may appear in a dataset at any time.
  Fields are not removed or retyped without a `catalog_version` bump, and a
  dataset being withdrawn is first marked `"deprecated": true` for at least one
  publish cycle before its entry is removed.
- **Append-mostly history.** Historical eras are not silently rewritten. A
  correction to closed history changes that era file's hash and its
  `updated_at`; it is a normal, detectable event, not a hidden one.

## Non-guarantees

- **No uptime or freshness SLA.** Publishing is best effort. `cadence` is an
  expectation, not a promise; an upstream outage shows up as an unchanged
  `updated_at`.
- **No accuracy warranty.** Data is redistributed as received. The upstream
  source is authoritative.
- **No stable ordering** of arrays within a dataset unless a schema says so.
- **No API.** There is no server here, only files in git. Rate limits and
  availability are GitHub's.

## How a consumer should behave

1. Fetch `catalog.json`. It is small; fetching it on every cycle is cheap.
2. Compare each `sha256` against what you already hold. Fetch only what differs.
3. Validate the fetched bytes against the catalogue's `sha256` before use.
4. **Never delete or overwrite good local data because a fetch failed.** An
   unreachable repository, a malformed catalogue, or a hash mismatch means *keep
   what you have* and report the failure. A publishing problem here must never
   destroy data already held downstream.
5. Treat an entry marked `deprecated` as still valid but scheduled for removal.

Point 4 is the important one. This repository is a source, not a master: it can
be empty, stale or briefly wrong, and nothing downstream should be worse off for
it than it was the day before.
