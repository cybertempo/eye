"""The browser tests run a current build: web/dist matches a fresh build of web/src.

scripts/verify.sh rebuilds web/dist before the tests; scripts/check_web_dist.py
compiles web/src separately and compares every file. Here the checker passes
the real build and a byte-identical copy (positive controls) and fails copies
with a changed, missing or extra file (negative controls). A checker that
cannot build its reference reports UNVERIFIED, never "current".
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

CHECKER = REPO_ROOT / "scripts" / "check_web_dist.py"
DIST = REPO_ROOT / "web" / "dist"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_web_dist", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_web_dist = load_checker()


@pytest.fixture(scope="module")
def fresh(tmp_path_factory) -> Path:
    """One reference build of the current web/src."""
    out = tmp_path_factory.mktemp("fresh") / "dist"
    check_web_dist.fresh_build(out)
    return out


@pytest.fixture
def copy(fresh, tmp_path) -> Path:
    target = tmp_path / "dist"
    shutil.copytree(fresh, target)
    return target


def run_checker(dist: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--dist", str(dist)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_real_build_is_current():
    assert (DIST / "app.js").is_file(), "run scripts/setup.sh"
    result = run_checker(DIST)
    assert result.returncode == 0, result.stderr


def test_identical_copy_passes(fresh, copy):
    assert check_web_dist.compare(fresh, copy) == []


def test_changed_file_fails(fresh, copy):
    target = copy / "facts.js"
    target.write_bytes(target.read_bytes() + b"\n// stale\n")
    assert check_web_dist.compare(fresh, copy) == ["differs from web/src: facts.js"]
    result = run_checker(copy)
    assert result.returncode == 1, result.stderr
    assert "facts.js" in result.stderr


def test_missing_file_fails(fresh, copy):
    (copy / "generated" / "wire-schema.js").unlink()
    assert check_web_dist.compare(fresh, copy) == ["missing: generated/wire-schema.js"]


def test_extra_file_fails(fresh, copy):
    (copy / "removed-module.js").write_text("export {};\n", encoding="utf-8")
    assert check_web_dist.compare(fresh, copy) == ["not built from web/src: removed-module.js"]


def test_absent_build_fails(fresh, tmp_path):
    assert check_web_dist.compare(fresh, tmp_path / "nowhere") != []
    assert run_checker(tmp_path / "nowhere").returncode == 1


def test_failed_reference_build_is_unverified(monkeypatch, copy, capsys):
    monkeypatch.setattr(check_web_dist, "TSC", REPO_ROOT / "web" / "no-such-tsc")
    assert check_web_dist.main(["--dist", str(copy)]) == 2
    assert "UNVERIFIED" in capsys.readouterr().err
