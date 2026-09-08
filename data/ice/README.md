# ICE

Exchange-certified coffee stocks data published by the Intercontinental Exchange.

- **Upstream:** https://www.ice.com/
- **Cadence:** daily on exchange days
- **Scope:** current certified stocks and the deep historical series, split into
  era files (see `docs/STRUCTURE.md`).
- **Processing:** parsed from the published report into JSON. Values are as
  printed; no adjustment, interpolation or derivation.

Datasets in this directory are indexed in `/catalog.json` under source `ice`.
