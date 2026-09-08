# Barchart

Futures price and options data sourced from Barchart.

- **Upstream:** https://www.barchart.com/
- **Cadence:** daily, with intraday series where noted per dataset
- **Scope:** price series and options data for the covered contracts.
- **Processing:** parsed into JSON with normalised field names and ISO 8601
  dates. Prices are as delivered; no adjustment or back-adjustment is applied
  unless a dataset's catalogue entry says so explicitly.

Fetching from the upstream requires an account. That credential lives in
repository secrets and is never committed. Reading the data published here
requires nothing.

Datasets in this directory are indexed in `/catalog.json` under source `barchart`.
