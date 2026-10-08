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
    assert json.loads(migrated.stdout) == {"applied_now": EXISTING, "database": "synthetic-demo"}
    loaded = run_eye("db-load-fixtures", url=url)
    assert loaded.returncode == 0, loaded.stderr
    first = json.loads(loaded.stdout)
    # 8 Package 1 + 4 synthetic AIS demo + 6 synthetic event-report batches
    assert first["created"] == first["batches"] == 23  # 5 of them news (Package 4d)
    again = json.loads(run_eye("db-load-fixtures", url=url).stdout)
    assert again["created"] == 0
    derived = run_eye("db-derive-transits", url=url)
    assert derived.returncode == 0, derived.stderr
    (line,) = json.loads(derived.stdout)["lines"]
    assert line["line"] == "synthetic-golden-gate/v1" and line["crossings"] == 3
    assert {
        "start": "2026-02-01T12:00:00.000000Z",
        "end": "2026-02-01T13:00:00.000000Z",
        "state": "qualified",
        "total": 1,
    } in line["counts"]
    replay = run_eye("db-replay", url=url)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert json.loads(replay.stdout) == {
        "completed_pending": 0,
        "discrepancies": [],
        "audited_runs": 1,
    }
    assert json.loads(run_eye("db-status", url=url).stdout) == {"applied": EXISTING, "pending": []}


def test_prepare_demo_is_one_repeatable_step(make_db):
    url = make_db()
    first = run_eye("db-prepare-demo", url=url)
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["applied_now"] == EXISTING and report["batches"] == 23
    assert len(report["runs"]) == 1
    again = json.loads(run_eye("db-prepare-demo", url=url).stdout)
    assert again["applied_now"] == [] and again["runs"] == report["runs"]  # no new run
    replay = run_eye("db-replay", url=url)
    assert replay.returncode == 0, replay.stdout + replay.stderr


def test_db_commands_refuse_missing_url_or_non_loopback_host(make_db):
    missing = run_eye("db-status", url=None)
    assert missing.returncode == 2 and "EYE_DATABASE_URL" in missing.stderr
    remote = run_eye("db-status", url="postgresql://eye@db.example.org/eye")
    assert remote.returncode == 2 and "loopback" in remote.stderr
    ok = run_eye("db-status", url=make_db())  # control
    assert ok.returncode == 0, ok.stderr


def run_lifecycle(*args: str, url: str, extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "backend"), EYE_DATABASE_URL=url, **extra)
    for name in ("EYE_BACKUP_DIR", "EYE_RESTORE_DATABASE_URL"):
        if name not in extra:
            env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "eye", *args, "--config", str(EXAMPLE_CONFIG)],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        cwd=REPO_ROOT,
    )


def test_lifecycle_commands_end_to_end_with_deletion_off(make_db, tmp_path):
    """Developer laptop or CI: the example configuration never prunes anything."""
    from eye.storage.db import connect

    url = make_db()
    assert run_eye("db-prepare-demo", url=url).returncode == 0
    store = {"EYE_BACKUP_DIR": str(tmp_path / "backup")}
    rollup = run_lifecycle("db-rollup", url=url, extra=store)
    assert rollup.returncode == 0, rollup.stderr
    assert all(m["error"] is None for m in json.loads(rollup.stdout)["manifests"])
    checked = run_lifecycle("db-manifest-check", url=url, extra=store)
    assert checked.returncode == 0, checked.stdout
    # Plan before any backup: every partition blocked, and nothing recorded.
    conn = connect(url)
    counts = (
        "SELECT (SELECT count(*) FROM eye.retention_decision), "
        "(SELECT count(*) FROM eye.manifest_validation)"
    )
    before = conn.run(counts)
    plan = run_lifecycle("db-retention-plan", url=url, extra=store)
    assert plan.returncode == 0, plan.stderr
    report = json.loads(plan.stdout)
    assert report["read_only"] is True and report["deletion_enabled"] is False
    assert {p["verdict"] for p in report["partitions"]} == {"blocked"}
    assert conn.run(counts) == before
    backed = run_lifecycle("db-backup", url=url, extra=store)
    assert backed.returncode == 0, backed.stderr
    backed_up = json.loads(backed.stdout)
    assert backed_up["state"] == "verified" and "SYNTHETIC" in backed_up["label"]
    plan = json.loads(run_lifecycle("db-retention-plan", url=url, extra=store).stdout)
    assert {p["verdict"] for p in plan["partitions"]} == {"eligible"}  # eligible, yet...
    refused = run_lifecycle(
        "db-retention-execute",
        "--partition",
        "synthetic-fixture:flight:2026-01-01",
        url=url,
        extra=store,
    )
    assert refused.returncode == 2 and "deletion is disabled" in refused.stderr  # ...off
    unnamed = run_lifecycle("db-retention-execute", url=url, extra=store)
    assert unnamed.returncode == 2 and "--partition" in unnamed.stderr
    assert conn.run("SELECT count(*) FROM eye.raw_evidence WHERE content IS NULL")[0][0] == 0
    drill = run_lifecycle(
        "db-restore-drill",
        "--generation",
        backed_up["generation_id"],
        url=url,
        extra={**store, "EYE_RESTORE_DATABASE_URL": make_db()},
    )
    assert drill.returncode == 0, drill.stdout + drill.stderr
    assert json.loads(drill.stdout)["state"] == "verified"
    damaged = run_lifecycle(
        "db-restore-drill",
        "--generation",
        "0" * 64,
        url=url,
        extra={**store, "EYE_RESTORE_DATABASE_URL": make_db()},
    )
    assert damaged.returncode == 5 and json.loads(damaged.stdout)["state"] == "failed"
    conn.close()
