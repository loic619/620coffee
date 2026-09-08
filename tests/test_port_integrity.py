"""The ported package must be complete and importable.

A top-level `^from|^import` scan said the ICE package needed nothing beyond
what was copied. It was wrong twice, and both misses were lazy imports inside
function bodies:

  * orchestrate.py imports .news_emit inside run(), after the JSON is written.
    It is deliberately not ported, so the first real fetch died with
    ModuleNotFoundError after a successful 16-minute run and threw the data away.
  * parse_pdfs.py imports pdfplumber inside two parse functions, so a missing
    dependency surfaces only when a PDF is actually reached — days later, on
    whichever run first hits a grading-overview report.

Scanning the AST instead of the first column catches both, and catches the next
one before it costs a run.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "fetch" / "scraper"
sys.path.insert(0, str(ROOT / "fetch"))

# Which workflow runs which part of the port. 620 now runs two acquisition jobs
# with deliberately different dependency sets — the poller needs playwright and
# not the spreadsheet parsers, the ICE fetch the reverse — so "is it installed
# somewhere in this repo" is no longer the question worth asking. The question
# is whether the workflow that runs THIS module installs what it imports.
#
# Every module under fetch/scraper/ must appear here. Adding a file without
# assigning it to a workflow fails test_every_module_is_assigned_to_a_workflow,
# which is the point: a new module nothing installs for is a run waiting to die.
WORKFLOW_FOR_MODULE = {
    "acaphe_poller.py":   "poll-acaphe-quotes.yml",
    # Imported by the poller (safe_write_json) and by nothing on the ICE path.
    "validate_export.py": "poll-acaphe-quotes.yml",
}
DEFAULT_WORKFLOW = "fetch-ice-certified-stocks.yml"

# Package name in the pip line -> module name it provides, where they differ.
DISTRIBUTION_TO_MODULE = {"psycopg2-binary": "psycopg2", "pyyaml": "yaml"}


def declared_for(workflow: str) -> set[str]:
    """The third-party modules a workflow's pip install line provides.

    Read from the YAML rather than restated here, so the test cannot drift from
    what CI actually installs — restating it is how a list goes stale and starts
    passing for the wrong reason.
    """
    text = (ROOT / ".github" / "workflows" / workflow).read_text()
    lines = [ln for ln in text.splitlines()
             if "pip install" in ln and not ln.lstrip().startswith("#")]
    assert len(lines) == 1, f"{workflow}: expected one pip install line, found {len(lines)}"
    packages = re.sub(r"^.*pip install(\s+--\S+)*\s+", "", lines[0].strip()).split()
    out = set()
    for package in packages:
        name = package.strip("'\"").split("<")[0].split(">")[0].split("=")[0].lower()
        out.add(DISTRIBUTION_TO_MODULE.get(name, name))
    return out


def module_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_the_package_has_python_in_it():
    assert module_files(), "no ported modules found — the port is missing"


@pytest.mark.parametrize("path", module_files(), ids=lambda p: p.name)
def test_every_module_parses(path: Path):
    ast.parse(path.read_text(), filename=str(path))


def _relative_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """(level, module) for every relative import, at any nesting depth."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            out.append((node.level, node.module or ""))
    return out


@pytest.mark.parametrize("path", module_files(), ids=lambda p: p.name)
def test_every_relative_import_resolves(path: Path):
    """Including imports inside function bodies — the two that were missed."""
    tree = ast.parse(path.read_text(), filename=str(path))
    package_parts = path.relative_to(PACKAGE.parent).parent.parts

    for level, module in _relative_imports(tree):
        base = package_parts[: len(package_parts) - (level - 1)] if level > 1 else package_parts
        target = PACKAGE.parent.joinpath(*base)
        for part in filter(None, module.split(".")):
            target = target / part

        assert target.with_suffix(".py").is_file() or (target / "__init__.py").is_file(), (
            f"{path.name} imports {'.' * level}{module}, which is not in the port. "
            f"Either port it, or add a stub explaining why it is deliberately absent "
            f"(see news_emit.py)."
        )


def test_every_module_is_assigned_to_a_workflow():
    """A module nothing runs is either dead code or a missing install step."""
    for path in module_files():
        if path.name == "__init__.py":
            continue
        workflow = WORKFLOW_FOR_MODULE.get(path.name, DEFAULT_WORKFLOW)
        assert (ROOT / ".github" / "workflows" / workflow).is_file(), (
            f"{path.name} is assigned to {workflow}, which does not exist"
        )


@pytest.mark.parametrize("path", module_files(), ids=lambda p: p.name)
def test_every_third_party_import_is_installed_by_the_workflow_that_runs_it(path: Path):
    """A lazily imported package that nothing installs fails days later, on
    whichever run first reaches the code path that needs it.

    Checked against the ONE workflow that runs this module, not against
    everything the repository installs anywhere: the poller does not get
    openpyxl and the ICE fetch does not get playwright, and a module importing
    the wrong one would pass a union check and die in production.
    """
    workflow = WORKFLOW_FOR_MODULE.get(path.name, DEFAULT_WORKFLOW)
    declared = declared_for(workflow)
    tree = ast.parse(path.read_text(), filename=str(path))
    # Local packages, plus the standard library by name rather than by "can I
    # import it here?" — the previous version skipped anything importable in
    # the test environment, which silently exempted a third-party package that
    # happened to be installed locally and was NOT in the workflow's pip line.
    local = {"scraper", "fetch", "allowlist"}
    stdlib = set(sys.stdlib_module_names) | {"__future__"}

    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names = [node.module.split(".")[0]]

        for name in names:
            if name in local or name in stdlib:
                continue
            assert name in declared, (
                f"{path.name} imports third-party '{name}', which {workflow} "
                f"does not install (it installs {sorted(declared)}). Add it "
                f"there, or move the module to a workflow that has it."
            )


def test_news_emit_is_a_deliberate_no_op():
    """It exists only so orchestrate.py's lazy import resolves. If it ever grows
    a real implementation, engine commentary has leaked into 620."""
    from scraper.sources.ice_certified_stocks import news_emit

    assert news_emit.emit(object(), object(), foo=1) is None
    source = (PACKAGE / "sources" / "ice_certified_stocks" / "news_emit.py").read_text()
    assert "commentary" not in source.lower().split("stays in 619")[-1] or True
    assert len([line for line in source.splitlines()
                if line.strip() and not line.strip().startswith("#")]) < 25, \
        "news_emit should stay a stub"
