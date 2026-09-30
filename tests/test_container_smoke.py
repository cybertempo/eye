"""Fault-inject the Docker cleanup step without starting a real container."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from conftest import REPO_ROOT

SMOKE = REPO_ROOT / "scripts" / "container-smoke.sh"


def demo_media(*, leak: bool = False) -> dict:
    """The demo's news items as the smoke check reads them: eight items, one with
    unknown rights (headline withheld unless ``leak``), one syndication suggestion."""
    ids = ["SN-A1", "SN-B1", "SN-C1", "SN-G1", "SN-L1", "SN-P1", "SN-V1", "SN-W1"]
    media = [
        {
            "item_id": item,
            "versions": [
                {
                    "rights": {"status": "unknown" if item == "SN-V1" else "link_only"},
                    "headline": "Leaked" if leak and item == "SN-V1" else None,
                }
            ],
        }
        for item in ids
    ]
    suggestion = {"items": ["a", "b"], "basis": "syndicated_copy", "status": "suggestion"}
    return {"media": media, "media_suggestions": [suggestion]}


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
    candidates = {"SYN-SL-1", "SYN-GAP-21", "SYN-TS-1"}
    events = [
        {"case_id": case, "standing": "review_candidate" if case in candidates else "report"}
        for case in (*sorted(candidates), "SYN-AV-1", "SYN-MC-1", "SYN-RD-1", "SYN-RD-2")
    ]
    view = {"events": events, **demo_media()}
    curl.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  *transits*) echo '{json.dumps({'counts': counts})}' ;;\n"
        f"  *snapshot*) echo '{json.dumps(view)}' ;;\n"
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
            assert "health, snapshot, event cases, news items and transit counts OK" in (
                result.stdout
            )


def test_smoke_refuses_a_candidate_shown_as_a_report(tmp_path: Path):
    """Negative control for the event check: a candidate relabelled as a report fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/bin/sh\ncase "$4" in\n  port) echo "127.0.0.1:8765" ;;\nesac\nexit 0\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    events = [
        {"case_id": case, "standing": "report"}
        for case in (
            "SYN-SL-1",
            "SYN-GAP-21",
            "SYN-TS-1",
            "SYN-AV-1",
            "SYN-MC-1",
            "SYN-RD-1",
            "SYN-RD-2",
        )
    ]
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  *snapshot*) echo '{json.dumps({'events': events})}' ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    env["EYE_DEMO_PORT"] = "8765"
    result = subprocess.run(
        [str(SMOKE)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode != 0
    assert "unexpected event cases" in result.stderr


def test_smoke_refuses_a_withheld_headline_that_is_shown(tmp_path: Path):
    """Negative control for the news check: an unknown-rights headline shown fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/bin/sh\ncase "$4" in\n  port) echo "127.0.0.1:8765" ;;\nesac\nexit 0\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    candidates = {"SYN-SL-1", "SYN-GAP-21", "SYN-TS-1"}
    events = [
        {"case_id": case, "standing": "review_candidate" if case in candidates else "report"}
        for case in (*sorted(candidates), "SYN-AV-1", "SYN-MC-1", "SYN-RD-1", "SYN-RD-2")
    ]
    curl = bin_dir / "curl"
    view = {"events": events, **demo_media(leak=True)}
    curl.write_text(
        f"#!/bin/sh\ncase \"$*\" in\n  *snapshot*) echo '{json.dumps(view)}' ;;\nesac\nexit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    env["EYE_DEMO_PORT"] = "8765"
    result = subprocess.run(
        [str(SMOKE)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode != 0
    assert "unexpected news items" in result.stderr
