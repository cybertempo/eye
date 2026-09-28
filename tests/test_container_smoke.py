"""Fault-inject the Docker cleanup step without starting a real container."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from conftest import REPO_ROOT

SMOKE = REPO_ROOT / "scripts" / "container-smoke.sh"


def test_cleanup_failure_refuses_success_and_clean_cleanup_passes(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$4" in\n'
        '  port) echo "127.0.0.1:8765" ;;\n'
        '  down) exit "$EYE_TEST_DOWN_STATUS" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    counts = [
        {"state": "qualified", "total": 1},
        {"state": "partial", "total": 1},
        {"state": "unknown", "total": None},
        {"state": "qualified", "total": 0},
    ]
    curl.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  *transits*) echo '{json.dumps({'counts': counts})}' ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    env["EYE_DEMO_PORT"] = "8765"
    for down_status, expected in (("0", 0), ("1", 1)):
        env["EYE_TEST_DOWN_STATUS"] = down_status
        result = subprocess.run(
            [str(SMOKE)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        if expected:
            assert "cleanup FAILED" in result.stderr
        else:
            assert "health, snapshot and transit counts OK" in result.stdout
