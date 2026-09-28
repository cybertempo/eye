"""The boundary checker fails on forbidden material and passes a clean tree."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

CHECKER = REPO_ROOT / "scripts" / "check_repo_boundary.py"
CLEAN_README = "# Demo\n\nNo licence has been chosen; reuse rights are not granted.\n"


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(root)], capture_output=True, text=True, timeout=30
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text(CLEAN_README, encoding="utf-8")
    (tmp_path / "NOTICE.md").write_text("# Notices\n\nNone yet.\n", encoding="utf-8")
    return tmp_path


def test_clean_tree_passes(repo):
    result = run_checker(repo)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("LICENSE", "MIT License\n"),
        (".env", "X=1\n"),
        ("config/server.pem", "x\n"),
        ("notes.md", "key = " + "AKIA" + "Q" * 16 + "\n"),
        ("notes.md", "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n"),
        ("notes.md", "host = " + "192.168" + ".1.20\n"),
        ("notes.md", "server = box." + "ts.net\n"),
        ("tool.py", "import " + "webbrowser\n"),
    ],
)
def test_forbidden_material_fails(repo, name, content):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    result = run_checker(repo)
    assert result.returncode == 1, result.stdout


def test_ignored_file_is_not_published(repo):
    (repo / ".gitignore").write_text("local.env\n", encoding="utf-8")
    (repo / "local.env").write_text("key = " + "AKIA" + "Q" * 16 + "\n", encoding="utf-8")
    assert run_checker(repo).returncode == 0


def test_staged_secret_is_checked_even_after_working_copy_is_clean(repo):
    target = repo / "notes.md"
    target.write_text("key = " + "AKIA" + "Q" * 16 + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "notes.md"], cwd=repo, check=True)
    target.write_text("public notes\n", encoding="utf-8")

    refused = run_checker(repo)
    assert refused.returncode == 1, refused.stdout
    assert "notes.md (staged)" in refused.stdout
    assert "AWS access key id" in refused.stdout

    subprocess.run(["git", "add", "notes.md"], cwd=repo, check=True)
    accepted = run_checker(repo)
    assert accepted.returncode == 0, accepted.stdout


def test_open_source_claim_fails_but_negation_passes(repo):
    readme = repo / "README.md"
    readme.write_text(CLEAN_README + "EYE is not open source.\n", encoding="utf-8")
    assert run_checker(repo).returncode == 0
    readme.write_text(CLEAN_README + "EYE is an open-source project.\n", encoding="utf-8")
    assert run_checker(repo).returncode == 1


def test_missing_licence_status_fails(repo):
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    assert run_checker(repo).returncode == 1


def test_not_a_repository_is_unverified(tmp_path):
    result = run_checker(tmp_path)
    assert result.returncode == 2
    assert "UNVERIFIED" in result.stderr
