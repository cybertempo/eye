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
    """Write a config dict to a temp file with the data paths made absolute."""

    def _write(raw: dict) -> Path:
        raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
        for key, value in raw.get("data", {}).items():
            if isinstance(value, str) and not Path(value).is_absolute():
                raw["data"][key] = str((EXAMPLE_CONFIG.parent / value).resolve())
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


AIS_DEMO = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "ais" / "demo"


class ApiHandle:
    """A running in-process API server over a prepared database."""

    def __init__(self, server, url: str, conn) -> None:
        self.server = server
        self.database_url = url
        self.conn = conn  # a writable connection for tests that add evidence
        self.host, self.port = server.server_address[:2]

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def get(self, path: str, headers: dict | None = None):
        import http.client

        client = http.client.HTTPConnection(self.host, self.port, timeout=10)
        client.request("GET", path, headers=headers or {})
        response = client.getresponse()
        body = response.read()
        client.close()
        return response, body

    def ingest(self, path: Path) -> None:
        from eye.ingest.capture import ingest

        ingest(self.conn, path.read_bytes())

    def derive(self) -> str:
        from eye.worker import transits

        line = transits.load_line(transits.LINES_DIR / "synthetic-golden-gate.v1.json")
        inputs = transits.read_inputs(self.conn, transits.SOURCE_ID, line)
        run_id, _ = transits.store(
            self.conn, transits.SOURCE_ID, line, transits.hourly_intervals(inputs)
        )
        return run_id


@pytest.fixture
def api_server(make_db, example_raw, write_config):
    """Start API servers on fresh databases holding the synthetic demo data.

    The feed hub's own polling is effectively off (60 s); tests call
    ``server.hub.tick()`` to deliver changes at a known moment.
    """
    import copy
    import threading

    from eye.api.auth import DemoAuth
    from eye.api.server import build_server
    from eye.config import load_config
    from eye.ingest.capture import load_fixtures
    from eye.storage.db import connect
    from eye.storage.migrate import migrate

    started = []

    def _start(*, api=None, server=None, ais_files=None, auth=None, mode_raw=None) -> ApiHandle:
        url = make_db()
        conn = connect(url)
        migrate(conn)
        load_fixtures(conn, CAPTURES)
        raw = copy.deepcopy(mode_raw or example_raw)
        raw["server"]["port"] = 0
        raw["api"].update({"poll_interval_ms": 60_000, **(api or {})})
        raw["server"].update(server or {})
        config = load_config(write_config(raw))
        handle_files = sorted(AIS_DEMO.glob("*.json")) if ais_files is None else ais_files
        srv = build_server(config, auth or DemoAuth("demo"), url)
        handle = ApiHandle(srv, url, conn)
        for path in handle_files:
            handle.ingest(path)
        handle.derive()
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        started.append((srv, conn))
        return handle

    yield _start
    import contextlib

    for srv, conn in started:
        srv.shutdown()
        srv.server_close()
        with contextlib.suppress(Exception):
            conn.close()  # a test may have dropped its database
