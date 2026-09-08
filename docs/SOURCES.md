# Sources

| Source key | Publisher | What is fetched | Cadence | Upstream |
|---|---|---|---|---|
| `ice` | Intercontinental Exchange | Certified coffee stock reports — the daily arabica and robusta report sets, plus the monthly ageing and age-allowance reports | Daily, exchange days | https://www.ice.com/ |

## Conventions

- **Window, not history.** Each run publishes the days it fetched. Records older
  than the window are trimmed before publication, including from structures that
  the parser accumulates internally.
- **Verbatim vs. tidied.** Reports are parsed out of their delivery format (XLS,
  CSV, PDF) into JSON with normalised field names and ISO 8601 dates. Values are
  not adjusted, filled, smoothed, rebased or derived. If a number differs from
  the upstream print, that is a bug.
- **Raw only.** The fetch code is shared with a private consumer and produces
  some derived fields in passing. `fetch/allowlist.py` names every field that
  may be published; anything unlisted is dropped, so a new field cannot leak by
  being unanticipated.
- **Credentials.** None. ICE publishes these reports openly.

## Adding a source

A source is added here only when moving its acquisition to free public Actions
measurably reduces metered private-repository usage. Publishing a dataset is not
in itself a reason to add one.
