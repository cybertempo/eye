"""The db-* commands against a real disposable database."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import EXAMPLE_CONFIG, REPO_ROOT
from eye.storage.migrate import discover

EXISTING = [m.version for m in discover()]


def run_eye(*args: str, url: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "backend"))
    env.pop("EYE_DATABASE_URL", None)
    if url is not None:
        env["EYE_DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "eye", *args, "--config", str(EXAMPLE_CONFIG)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=REPO_ROOT,
    )


def test_db_commands_end_to_end(make_db):
    url = make_db()
    migrated = run_eye("db-migrate", url=url)
    assert migrated.returncode == 0, migrated.stderr
    assert json.loads(migrated.stdout) == {"applied_now": EXISTING}
    loaded = run_eye("db-load-fixtures", url=url)
    assert loaded.returncode == 0, loaded.stderr
    first = json.loads(loaded.stdout)
    assert first["created"] == first["batches"] == 9  # 8 Package 1 + 1 synthetic AIS
    again = json.loads(run_eye("db-load-fixtures", url=url).stdout)
    assert again["created"] == 0
    derived = run_eye("db-derive-transits", url=url)
    assert derived.returncode == 0, derived.stderr
    (line,) = json.loads(derived.stdout)["lines"]
    assert line["line"] == "synthetic-golden-gate/v1" and line["crossings"] == 1
    assert {
        "start": "2026-02-01T12:00:00.000000Z",
        "end": "2026-02-01T13:00:00.000000Z",
        "state": "qualified",
        "total": 1,
    } in line["counts"]
    replay = run_eye("db-replay", url=url)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert json.loads(replay.stdout) == {"completed_pending": 0, "discrepancies": []}
    assert json.loads(run_eye("db-status", url=url).stdout) == {"applied": EXISTING, "pending": []}


def test_db_commands_refuse_missing_url_or_non_loopback_host(make_db):
    missing = run_eye("db-status", url=None)
    assert missing.returncode == 2 and "EYE_DATABASE_URL" in missing.stderr
    remote = run_eye("db-status", url="postgresql://eye@db.example.org/eye")
    assert remote.returncode == 2 and "loopback" in remote.stderr
    ok = run_eye("db-status", url=make_db())  # control
    assert ok.returncode == 0, ok.stderr
