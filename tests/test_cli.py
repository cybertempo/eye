"""The command-line entry point refuses unsafe configuration with distinct exit codes."""

from __future__ import annotations

import copy
import os
import subprocess
import sys

from conftest import EXAMPLE_CONFIG, REPO_ROOT


def run_eye(*args: str, database_url: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("EYE_DATABASE_URL", None)
    if database_url is not None:
        env["EYE_DATABASE_URL"] = database_url
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "backend"), str(REPO_ROOT / "tests" / "support")]
    )
    env.pop("EYE_CONTAINER", None)
    return subprocess.run(
        [sys.executable, "-m", "eye", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=REPO_ROOT,
    )


def test_example_config_accepted():
    result = run_eye("check-config", "--config", str(EXAMPLE_CONFIG))
    assert result.returncode == 0, result.stderr
    assert "mode=demo" in result.stdout


def test_public_bind_refused_before_listening(example_raw, write_config):
    example_raw["server"]["bind_host"] = "0.0.0.0"
    result = run_eye("serve", "--config", str(write_config(example_raw)))
    assert result.returncode == 2
    assert "REFUSED (configuration)" in result.stderr
    assert "listening" not in result.stdout


def test_production_without_adapter_refused(example_raw, write_config):
    raw = copy.deepcopy(example_raw)
    raw["runtime"]["mode"] = "production"
    result = run_eye("serve", "--config", str(write_config(raw)))
    assert result.returncode == 2
    assert "never a production fallback" in result.stderr


def test_production_with_uninstalled_adapter_refused(example_raw, write_config):
    raw = copy.deepcopy(example_raw)
    raw["runtime"]["mode"] = "production"
    raw["auth"] = {"adapter": "private", "private_adapter_module": "eye_private_missing"}
    result = run_eye("serve", "--config", str(write_config(raw)))
    assert result.returncode == 3
    assert "REFUSED (authentication)" in result.stderr


def test_production_with_adapter_passes_auth_gate_then_needs_its_database(
    example_raw, write_config
):
    raw = copy.deepcopy(example_raw)
    raw["runtime"]["mode"] = "production"
    raw["auth"] = {"adapter": "private", "private_adapter_module": "eye_test_private_auth"}
    path = str(write_config(raw))
    checked = run_eye("check-config", "--config", path)
    assert checked.returncode == 0, checked.stderr
    assert "auth=test-private" in checked.stdout
    # Past the authentication gate, production serves database-backed data only.
    no_database = run_eye("serve", "--config", path)
    assert no_database.returncode == 2
    assert "EYE_DATABASE_URL is not set" in no_database.stderr
    unreachable = run_eye(
        "serve", "--config", path, database_url="postgresql://eye:x@127.0.0.1:1/eye"
    )
    assert unreachable.returncode == 4
    assert "database unreachable" in unreachable.stderr
