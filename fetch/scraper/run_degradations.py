"""run_degradations.py — green runs that came home with less than they asked for.

The data-map run record reads GitHub's conclusion for every workflow, and a
conclusion is the wrong instrument for this class of fault. Three times in one
session a job here exited 0 while collecting nothing: an ICE run refused on
every request that merged nothing and wrote both JSONs; a population fetch whose
fix the next export silently overwrote; a 403 bail-out that short-circuited the
rest of a run without tripping the wholly-refused guard, because earlier
requests had succeeded. Every one of them was green in the Actions tab, green in
the activity panel, and wrong.

Skipping work is often the RIGHT call — one refused source must not throw away
the nine that served — so the answer is not to fail those runs. It is to stop
letting "green" and "complete" be the same word. A workflow that decides to skip
part of its work writes a row here, and the panel reads it beside the
conclusions:

    success                          the run did what it said
    success + a row in this file     the run finished, minus this much

That second state is the one that has cost this project the most data, and until
now nothing rendered it anywhere.

Output: frontend/public/data/run_degradations.json — committed by the workflow
that wrote it, so the published site carries it like any other data file.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "frontend" / "public" / "data" / "run_degradations.json"

# The data-map panel shows a 7-day window; keep a month so a recurring block is
# still legible after the panel has moved on, and cap the rows so an unattended
# bad week cannot grow the file without bound.
KEEP_DAYS = 30
KEEP_ROWS = 200


def _load() -> list[dict]:
    try:
        return json.loads(PATH.read_text(encoding="utf-8")).get("runs", [])
    except Exception:
        return []


def record(*, workflow: str, file: str, kind: str, detail: str,
           items: list[str] | None = None) -> dict | None:
    """Note that this run finished green having skipped some of its work.

    `file` is the workflow's YAML basename because that is what the activity
    panel joins on — a display title is mutable and the Actions API caches it
    per run, which once reported eight healthy jobs as silent after a renaming
    pass. `kind` is a short slug ("http_403"); `detail` is the sentence a person
    reads; `items` names what was skipped.

    Idempotent per GitHub run id: a retried attempt replaces its own row rather
    than appending a second one. Never raises — a telemetry write must not be
    able to fail a run that otherwise succeeded.
    """
    try:
        run_id = os.environ.get("GITHUB_RUN_ID") or "local"
        now = datetime.now(UTC)
        row = {
            "at": now.isoformat(timespec="seconds"),
            "date": now.date().isoformat(),
            "workflow": workflow,
            "file": file,
            "run_id": run_id,
            "run_url": (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}"
                        if os.environ.get("GITHUB_REPOSITORY") and run_id != "local" else None),
            "kind": kind,
            "detail": detail,
            "items": items or [],
        }
        cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
        rows = [r for r in _load()
                if r.get("date", "") >= cutoff and r.get("run_id") != run_id]
        rows.append(row)
        rows = sorted(rows, key=lambda r: r.get("at", ""))[-KEEP_ROWS:]
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(
            json.dumps({"generated_at": now.isoformat(timespec="seconds"),
                        "keep_days": KEEP_DAYS, "runs": rows}, indent=1) + "\n",
            encoding="utf-8")
        return row
    except Exception as e:  # noqa: BLE001 — telemetry must never break the run
        print(f"  ! run-degradation write failed: {e}")
        return None
