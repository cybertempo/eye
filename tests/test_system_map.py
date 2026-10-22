"""The architecture map's generator reads the repository's facts and refuses to guess."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

GENERATOR = REPO_ROOT / "scripts" / "gen_system_map.py"


def run(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(GENERATOR), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        timeout=60,
    )


def facts_of(root: Path) -> dict:
    result = run(root)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def repo_facts() -> dict:
    return facts_of(REPO_ROOT)


def by_path(facts: dict) -> dict[str, dict]:
    return {f["path"]: f for f in facts["files"]}


def test_repository_facts(repo_facts):
    files = by_path(repo_facts)
    assert repo_facts["format"] == "eye.system-map-facts/1"
    assert repo_facts["cli_commands"][:2] == ["check-config", "serve"]
    assert "db-backtest-replay" in repo_facts["cli_commands"]
    assert "/api/v0/stream" in repo_facts["routes"]
    assert 4 in repo_facts["wire_versions"]
    first = files["migrations/0001_capture_coverage_observation_evidence.sql"]
    assert {"capture_batch", "raw_evidence", "coverage"} <= set(first["tables"])
    assert "observation_version" in first["views"]
    assert files["backend/eye/ingest/capture.py"]["loc"] > 0
    assert "web/src/live.ts" in files["web/src/app.ts"]["imports"]


def test_submodule_import_names_the_module_not_its_package(repo_facts):
    feed = by_path(repo_facts)["backend/eye/api/feed.py"]["imports"]
    assert "backend/eye/api/events.py" in feed  # from eye.api import events
    assert "backend/eye/api/__init__.py" not in feed


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_unparseable_file_is_unverified_beside_a_parsed_one(tmp_path):
    write(tmp_path, "backend/eye/__init__.py", "")
    write(tmp_path, "backend/eye/good.py", '"""Parses fine."""\nfrom eye import bad\n')
    write(tmp_path, "backend/eye/bad.py", "def broken(:\n")
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    files = by_path(json.loads(result.stdout))
    assert files["backend/eye/good.py"]["doc"] == "Parses fine."
    assert files["backend/eye/good.py"]["imports"] == ["backend/eye/bad.py"]
    assert files["backend/eye/bad.py"]["parse_error"] is True
    assert "1 UNVERIFIED" in result.stderr


def test_commented_out_table_is_not_a_table(tmp_path):
    write(tmp_path, "backend/eye/__init__.py", "")
    write(
        tmp_path,
        "migrations/0011_queue.sql",
        "-- 0011: queue\n-- CREATE TABLE eye.not_real (id int);\n"
        "CREATE TABLE eye.job (id int);\nCREATE VIEW eye.open_job AS SELECT 1;\n",
    )
    sql = by_path(facts_of(tmp_path))["migrations/0011_queue.sql"]
    assert sql["tables"] == ["job"]
    assert sql["views"] == ["open_job"]
    assert sql["doc"] == "0011: queue"


def test_refuses_a_directory_that_is_not_an_eye_checkout(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 2
    assert "not an EYE checkout" in result.stderr
    write(tmp_path, "backend/eye/__init__.py", "")
    assert run(tmp_path).returncode == 0
