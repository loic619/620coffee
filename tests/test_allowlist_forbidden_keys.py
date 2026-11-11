"""The arabica ageing reconstruction can never be published by 620.

The 619 sync (PR #23) builds `ageing_history`, `ageing_cohorts` and
`ageing_history_rejected` in memory. publish_ice.py prunes them, but the
allow-list check is the independent control, so it names them too.

Each test runs scripts/check_allowlist.py against a copy of the payloads
actually committed under data/ice/, so the baseline is a real published file.
Positive control: the clean copy passes, and the pre-existing forbidden key
`port_peaks` is caught by the same path, so a pass for the new keys would mean
the detector works and the keys are absent, not that nothing was checked.
"""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NEW_KEYS = ("ageing_history", "ageing_cohorts", "ageing_history_rejected")


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_allowlist", REPO / "scripts" / "check_allowlist.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def staged(tmp_path, monkeypatch):
    checker = _load_checker()
    ice = tmp_path / "data" / "ice"
    ice.mkdir(parents=True)
    copied = []
    for market in checker.SPECS:
        src = REPO / "data" / "ice" / f"{market}_latest.json"
        if src.is_file():
            shutil.copy(src, ice / src.name)
            copied.append(src.name)
    assert copied, "no published payload to build on"
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    return checker, ice / copied[0]


def _inject(path: Path, key: str) -> None:
    doc = json.loads(path.read_text())
    # Nested, where a merge slip would most plausibly leave it.
    doc["snapshots"][0][key] = [{"date": "2026-11-10", "bags": 1}]
    path.write_text(json.dumps(doc))


def test_new_keys_are_forbidden():
    forbidden = _load_checker().FORBIDDEN_KEYS
    for key in NEW_KEYS:
        assert key in forbidden


def test_clean_copy_passes(staged):
    checker, _ = staged
    assert checker.main() == 0


def test_existing_forbidden_key_is_caught(staged, capsys):
    # Positive control for the detector itself.
    checker, path = staged
    _inject(path, "port_peaks")
    assert checker.main() == 1
    assert "'snapshots.[].port_peaks' is private" in capsys.readouterr().err


@pytest.mark.parametrize("key", NEW_KEYS)
def test_ageing_key_is_caught(staged, capsys, key):
    checker, path = staged
    _inject(path, key)
    assert checker.main() == 1
    assert f"'snapshots.[].{key}' is private" in capsys.readouterr().err
