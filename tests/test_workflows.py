"""Workflow files that parse, create jobs, and keep the fetch's operating limits.

A workflow that GitHub rejects does not fail loudly — it creates NO JOBS AT ALL
and the run simply never happens. This repository has already produced one:
`if: ${{ secrets.X != '' }}` is invalid, because the `secrets` context is not
available in a step-level `if`, and it invalidated the entire file rather than
that one step. Nothing in CI noticed. These tests are the thing that notices.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent
                    / ".github" / "workflows").glob("*.yml"))
FETCH = ".github/workflows/fetch-ice-certified-stocks.yml"


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text())
    # PyYAML resolves an unquoted `on:` key to the boolean True.
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def test_there_are_workflows_to_check():
    assert WORKFLOWS, "no workflow files found — these tests would be vacuous"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_workflow_parses_and_declares_at_least_one_job(path):
    doc = _load(path)
    assert isinstance(doc, dict), f"{path.name} is not a mapping"
    assert doc.get("on"), f"{path.name} declares no triggers, so it can never run"
    jobs = doc.get("jobs")
    assert jobs, f"{path.name} declares no jobs — a run of it would do nothing"
    for name, job in jobs.items():
        assert job.get("steps"), f"{path.name}: job '{name}' has no steps"
        assert job.get("runs-on"), f"{path.name}: job '{name}' has no runner"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_step_condition_reads_the_secrets_context(path):
    """The failure that produced zero jobs. `secrets` is not available in a
    step-level `if:`, and using it there invalidates the whole file. Guard in
    the shell instead — see the fetch workflow for the shape."""
    for job in _load(path).get("jobs", {}).values():
        for step in job.get("steps", []):
            condition = str(step.get("if", ""))
            assert "secrets." not in condition, (
                f"{path.name}: step '{step.get('name', '?')}' reads secrets in "
                f"its `if:` — GitHub rejects the entire workflow file for this"
            )


# ── the fetch workflow's operating limits ────────────────────────────────────

@pytest.fixture
def fetch_job():
    doc = _load(Path(__file__).resolve().parent.parent / FETCH)
    return doc, doc["jobs"]["fetch"]


def test_the_fetch_timeout_still_fits_a_full_sweep(fetch_job):
    """1,810 candidates x 4s = 121 minutes. Pinned against the sweep in
    test_pacing_baseline.py, which asserts the same relationship from the
    other end."""
    _, job = fetch_job
    assert job["timeout-minutes"] == 150


def test_the_fetch_serialises_against_itself(fetch_job):
    """Two fetches at once would race on the same committed hits log and
    payload. Queue, never cancel: a dropped scheduled run loses a day."""
    doc, _ = fetch_job
    assert doc["concurrency"]["group"] == "fetch-ice-certified-stocks"
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_the_fetch_needs_no_secret(fetch_job):
    """Tier 0 and tier 1 are served by the committed dated log, not by
    repository configuration. Nothing here may depend on a secret."""
    text = (Path(__file__).resolve().parent.parent / FETCH).read_text()
    # Comments discuss the absence of secrets, and check_no_secrets.py is a
    # script name. A real reference is only ever `${{ ... secrets.NAME ... }}`.
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    found = re.findall(r"\$\{\{[^}]*\bsecrets\.[A-Za-z_][A-Za-z0-9_]*", code)
    assert not found, f"the fetch workflow has grown a secret dependency: {found}"
    assert "ICE_TIER1_HINTS" not in code


def test_the_fetch_never_runs_on_a_contributed_branch(fetch_job):
    """It has `contents: write` and pushes to main. A pull_request trigger
    would hand that to anyone who can open a PR."""
    doc, _ = fetch_job
    assert set(doc["on"]) <= {"schedule", "workflow_dispatch"}


def test_the_fetch_commits_the_learned_hits_log(fetch_job):
    """The one piece of fetch state that must survive the runner. Without this
    620 relearns tier 1 from the bootstrap guesses on every run and tier 0 is
    permanently empty for anything this repo captured itself."""
    text = (Path(__file__).resolve().parent.parent / FETCH).read_text()
    assert "stock_report_hits.json" in text
    assert "git add \"$HITS\"" in text


def test_the_hits_log_is_not_git_ignored():
    """Committing it in the workflow is no use if .gitignore drops it first."""
    root = Path(__file__).resolve().parent.parent
    patterns = [line.strip() for line in (root / ".gitignore").read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    assert not [p for p in patterns if "stock_report_hits.json" in p], (
        "stock_report_hits.json is git-ignored — it is learned state and must "
        "survive across runs"
    )
    assert (root / "fetch" / "scraper" / "sources" / "ice_certified_stocks"
            / "stock_report_hits.json").exists()
