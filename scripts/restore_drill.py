#!/usr/bin/env python3
"""Run the synthetic backup and restore drill in disposable databases.

Runs on: developer laptop, CI (called by scripts/restore-drill.sh). Needs
EYE_TEST_DATABASE_URL naming a disposable PostGIS server; it creates two
databases there and drops them afterwards. SYNTHETIC CODE TEST ONLY.
Exit status: 0 the restore reproduced every named metric, 1 it did not,
2 the drill could not run (UNVERIFIED).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    from eye.ingest.capture import load_fixtures
    from eye.storage.db import connect
    from eye.storage.migrate import migrate
    from eye.worker import backup, rollups, transits

    admin_url = os.environ.get("EYE_TEST_DATABASE_URL")
    if not admin_url:
        print("restore-drill: UNVERIFIED, EYE_TEST_DATABASE_URL is not set", file=sys.stderr)
        return 2
    admin = connect(admin_url, require_loopback=True)
    names = [f"eye_drill_{uuid.uuid4().hex[:12]}" for _ in range(2)]
    for name in names:
        admin.run(f'CREATE DATABASE "{name}"')
    urls = [urlunsplit(urlsplit(admin_url)._replace(path=f"/{n}")) for n in names]
    try:
        source = connect(urls[0], require_loopback=True)
        migrate(source)
        fixtures = ROOT / "tests" / "fixtures" / "synthetic"
        for name in ("captures", "ais/demo", "events/demo", "media/demo"):
            load_fixtures(source, fixtures / name)
        lines_dir = ROOT / "reference" / "lines"
        lines = [transits.load_line(p) for p in sorted(lines_dir.glob("*.json"))]
        for line in lines:
            inputs = transits.read_inputs(source, transits.SOURCE_ID, line)
            transits.store(source, transits.SOURCE_ID, line, transits.hourly_intervals(inputs))
        refused = [r for r in rollups.refresh_all(source, lines) if r.error]
        with tempfile.TemporaryDirectory(prefix="eye-synthetic-backup-") as store:
            generation = backup.export(source, Path(store), lines_dir)
            proof = backup.verify(source, Path(store), generation)
            target = connect(urls[1], require_loopback=True)
            report = backup.restore_drill(Path(store), generation, target)
            target.close()
        source.close()
        result = {
            **report.as_dict(),
            "generation_id": generation,
            "backup_proof": proof.state,
            "refused_rollups": [r.error for r in refused],
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        ok = report.state == "verified" and proof.state == "verified" and not refused
        return 0 if ok else 1
    finally:
        for name in names:
            admin.run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "backend"))
    sys.exit(main())
