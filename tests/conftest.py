from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "eye.example.toml"


@pytest.fixture
def example_raw() -> dict:
    """A fresh parsed copy of the example configuration."""
    with EXAMPLE_CONFIG.open("rb") as handle:
        return tomllib.load(handle)


def dump_toml(raw: dict) -> str:
    """Minimal TOML writer for the flat tables used by EYE configuration."""

    def value(v: object) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(i) for i in v) + "]"
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [f"{k} = {value(v)}" for k, v in raw.items() if not isinstance(v, dict)]
    for table, entries in raw.items():
        if isinstance(entries, dict):
            lines.append(f"[{table}]")
            lines.extend(f"{k} = {value(v)}" for k, v in entries.items())
    return "\n".join(lines) + "\n"


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a config dict to a temp file with the fixture path made absolute."""

    def _write(raw: dict) -> Path:
        raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
        fixture = raw.get("data", {}).get("fixture")
        if fixture and not Path(fixture).is_absolute():
            raw["data"]["fixture"] = str((EXAMPLE_CONFIG.parent / fixture).resolve())
        path = tmp_path / "eye.toml"
        path.write_text(dump_toml(raw), encoding="utf-8")
        return path

    return _write


CAPTURES = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "captures"


@pytest.fixture(scope="session")
def admin_url() -> str:
    """URL of a disposable PostGIS server (scripts/test-db.sh provides one).

    Without it, database tests are skipped and labelled UNVERIFIED, and
    scripts/verify.sh (which sets EYE_REQUIRE_DB=1) fails instead of skipping.
    """
    import os

    url = os.environ.get("EYE_TEST_DATABASE_URL")
    if not url:
        message = "UNVERIFIED: no EYE_TEST_DATABASE_URL; run via scripts/test-db.sh"
        if os.environ.get("EYE_REQUIRE_DB") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return url


@pytest.fixture
def make_db(admin_url):
    """Create empty databases on the test server; drop them afterwards."""
    import uuid
    from urllib.parse import urlsplit, urlunsplit

    from eye.storage.db import connect

    admin = connect(admin_url)
    created: list[str] = []

    def _make() -> str:
        name = f"eye_t_{uuid.uuid4().hex[:12]}"
        admin.run(f'CREATE DATABASE "{name}"')
        created.append(name)
        parts = urlsplit(admin_url)
        return urlunsplit(parts._replace(path=f"/{name}"))

    yield _make
    for name in created:
        admin.run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture
def db(make_db):
    """A connection to a fresh, fully migrated database."""
    from eye.storage.db import connect
    from eye.storage.migrate import migrate

    conn = connect(make_db())
    migrate(conn)
    yield conn
    conn.close()
