"""O66: the browser tests must run the current source, never a stale bundle.

The browser tests load web/dist. scripts/verify.sh rebuilds it and then runs
scripts/check_web_dist.py, which compiles web/src afresh and compares. Each
negative (a stale, missing or left-over file) has a fresh build beside it as
the positive control, on a copy of web/ so the repository's own dist is never
touched.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from check_web_dist import stale  # noqa: E402

WEB = REPO_ROOT / "web"


@pytest.fixture
def web(tmp_path) -> Path:
    """A copy of web/ (source and config; node_modules linked) with no dist."""
    copy = tmp_path / "web"
    copy.mkdir()
    shutil.copytree(WEB / "src", copy / "src")
    for name in ("tsconfig.json", "package.json"):
        shutil.copy2(WEB / name, copy / name)
    (copy / "node_modules").symlink_to(WEB / "node_modules")
    return copy


def build(web: Path) -> None:
    subprocess.run(
        [str(web / "node_modules" / ".bin" / "tsc"), "-p", "tsconfig.json"], cwd=web, check=True
    )


def test_a_fresh_build_passes(web):
    build(web)
    assert stale(web) == []


def test_source_changed_after_the_build_is_detected(web):
    build(web)
    live = web / "src" / "live.ts"
    live.write_text(live.read_text() + "\nexport const O66_MARKER = 1;\n")
    assert stale(web) == ["dist/live.js differs from a fresh build of web/src"]
    build(web)  # control: rebuilding clears it
    assert stale(web) == []


def test_a_missing_bundle_is_detected(web):
    assert "dist/app.js is missing" in stale(web)  # never built
    build(web)
    (web / "dist" / "desk.js").unlink()
    assert stale(web) == ["dist/desk.js is missing"]


def test_a_file_left_over_from_deleted_source_is_detected(web):
    build(web)
    (web / "dist" / "removed.js").write_text("export const old = 1;\n")
    assert stale(web) == ["dist/removed.js has no source (left over from deleted code)"]


def test_source_that_does_not_compile_is_reported(web):
    build(web)
    (web / "src" / "broken.ts").write_text("export const x: number = 'text';\n")
    (problem,) = stale(web)
    assert problem.startswith("web/src does not compile")


def test_the_command_exits_non_zero_on_a_stale_bundle(web):
    script = [sys.executable, str(REPO_ROOT / "scripts" / "check_web_dist.py"), "--web", str(web)]
    build(web)
    assert subprocess.run(script, capture_output=True, check=False).returncode == 0
    (web / "dist" / "app.js").write_text("// stale\n")
    failed = subprocess.run(script, capture_output=True, text=True, check=False)
    assert failed.returncode == 1 and "dist/app.js differs" in failed.stderr


def test_verify_rebuilds_and_checks_before_the_tests():
    lines = (REPO_ROOT / "scripts" / "verify.sh").read_text().splitlines()
    position = {name: i for i, line in enumerate(lines) for name in [line.strip()]}
    clean = position["rm -rf web/dist"]
    rebuild = position["npm run --prefix web build"]
    check = position['"$PY" scripts/check_web_dist.py']
    tests = next(i for i, line in enumerate(lines) if "-m pytest" in line)
    assert clean < rebuild < check < tests


def test_the_repository_bundle_matches_its_source():
    """Under verify this follows the rebuild; run alone, it catches a stale dist."""
    assert stale(WEB) == []
