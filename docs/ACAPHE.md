# Acaphe live quotes

620 polls acaphe.com and pushes each snapshot to Upstash Redis. 619's frontend
reads Redis and is unchanged by this move.

## Why it moved

It is acquisition, and it is the highest-frequency job either repository runs —
roughly 50 ticks a week, each capable of launching a browser. Free on a public
repository, metered on a private one. Redis is reached identically from either,
so the live path does not change.

## What runs

`.github/workflows/poll-acaphe-quotes.yml` → `fetch/scraper/acaphe_poller.py
--once`. Both cron blocks are 619's, unchanged, and cron-job.org dispatches the
workflow externally as well because GitHub's scheduler has throttled this
workflow twice.

`acaphe_poller.py` differs from 619's copy in **three** places, all marked
`PORTED TO 620`: the module docstring, and the two `OUTPUT` / `VIETNAM_LAST`
path anchors. Everything else is byte-identical on purpose — Redis is the live
path for both, so a difference in what arrives there would have to mean the poll
differed, not the code. `validate_export.py` is byte-identical.

## What is deliberately NOT here

**The quote files are not committed.** 619 writes `acaphe_live.json` and
`vietnam_last.json` into its frontend's public data directory, where the panel
reads them as a last-resort fallback. Here they are written to a git-ignored
`_stage/acaphe/` and die with the runner.

That is a redistribution decision, not an oversight. Every other dataset in this
repository comes from a document its publisher serves openly; acaphe is behind a
**login**. Committing login-gated quotes to a public repository is a call for
the owner to make explicitly, and the switch does not need it: Redis carries the
live path, and the `vietnam_last` Redis key carries the VN fallback.

What is lost is only the third-level fallback — the committed file 619 reads
when Redis itself is empty. 619's panel already degrades honestly there
(`FALLBACK_MAX_AGE_S`, the `DELAYED — fallback snapshot` badge), so the failure
mode is visible rather than silent. If that file should keep updating, the fix
is for 620 to publish it under `data/acaphe/` and 619 to consume it the way it
consumes the ICE window — not for the poller to commit into a frontend that does
not exist here.

**`DATABASE_URL` is not set, and must not be.** The Postgres behind it is 619's
private history. `_save_vn_prices_to_db` is guarded by `if DATABASE_URL:`, so
with the variable unset that path is never entered — which is why this workflow
installs `requests playwright` and not 619's additional `sqlalchemy` and
`psycopg2-binary`. 619's comment warns those are *not* optional there, and that
reasoning does not carry over: unset means never called, rather than called and
failing silently.

The consequence is real and worth stating: **the VN daily Postgres row is not
written by 620.** While 619's own poller still runs, 619 keeps writing it. Once
619's poller is switched off, that history stops accruing unless 619 persists it
from the `vietnam_last` Redis key on its own schedule.

## Secrets

`UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, `ACAPHE_USER`,
`ACAPHE_PASS`.

This repository is public, so the workflow triggers on `schedule` and
`workflow_dispatch` only — never `pull_request` — and reads secrets through
`env:`, never a step-level `if:`. Both are asserted in
`tests/test_workflows.py`. The `if: ${{ secrets.X != '' }}` form is not a soft
failure: GitHub rejects the entire workflow file and creates no jobs at all.
