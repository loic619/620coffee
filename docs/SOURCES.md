# Sources

Provenance for every dataset family published here. One row per upstream
publisher; per-dataset detail is in `catalog.json`.

| Source key | Publisher | What is taken | Cadence | Upstream |
|---|---|---|---|---|
| `ice` | Intercontinental Exchange | Certified/exchange-certified coffee stocks reports, current and historical | Daily (exchange days) | https://www.ice.com/ |
| `cftc` | U.S. Commodity Futures Trading Commission | Commitments of Traders positioning reports | Weekly | https://www.cftc.gov/MarketReports/CommitmentsofTraders/ |
| `barchart` | Barchart | Futures price series and options data | Daily / intraday | https://www.barchart.com/ |
| `acaphe` | Acaphe | Vietnamese physical coffee quotes | Daily | https://acaphe.com/ |

## Conventions

- **Attribution.** Where a source asks to be credited, the credit line is in that
  source's directory README and is reproduced with any onward use.
- **Terms.** Everything published here is released under CC0 1.0 (see the root
  `LICENSE`); no condition is placed on reuse.
- **Verbatim vs. tidied.** Data is stored tidied — parsed out of its delivery
  format (HTML table, CSV, report page) into JSON, with field names normalised
  and dates in ISO 8601. Values are not adjusted, filled, smoothed, rebased or
  derived. If a number differs from the upstream print, that is a bug.
- **Credentials.** Some upstreams require an account to *fetch*. No credential is
  ever committed here; publishing workflows read them from repository secrets.
  Nothing in this repository requires a credential to *read*.

## Adding a source

Add a row above, create `data/<key>/README.md` from the pattern used by the
existing ones, and record each dataset in `catalog.json`.
