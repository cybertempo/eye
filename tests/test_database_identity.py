"""Demo mode refuses a private or real database before serving or writing.

The configured mode and a loopback connection do not show which database is on
the other end, so the database records its kind (migration 0009). Each refusal
sits beside a positive control on a synthetic demo database.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest
from conftest import CAPTURES, REPO_ROOT
from eye.api.server import StartupError, check_database
from eye.config import load_config
from eye.ingest.capture import load_fixtures
from eye.storage import identity
from eye.storage.db import connect, transaction
from eye.storage.migrate import discover, migrate
from pg8000.exceptions import DatabaseError

# Every demo-mode command that would migrate, load, derive, delete or record.
DEMO_WRITERS = (
    ("db-migrate",),
    ("db-prepare-demo",),
    ("db-load-fixtures",),
    ("db-derive-transits",),
    ("db-rollup",),
    ("db-retention-execute", "--partition", "synthetic-fixture:flight:2026-01-15"),
    ("db-backtest", "--series", "synthetic-fixture:vessel:observations-hourly",
     "--start", "2026-01-15", "--end", "2026-01-15"),
)  # fmt: skip
REAL_BATCH = (
    "INSERT INTO eye.capture_batch (batch_id, source_id, layer, adapter_version, capture_format, "
    "requested_area, requested_start, requested_end, expected_interval_s, attempt_started_at, "
    "attempt_finished_at, provider_status, quota_cost, evidence_sha256) VALUES "
    "(gen_random_uuid(), 'real-provider', 'vessel', 'real/1', 'positions/1', "
    "ST_MakeEnvelope(0, 0, 1, 1, 4326), '2026-01-01T00:00Z', '2026-01-01T01:00Z', 60, "
    "'2026-01-01T01:00Z', '2026-01-01T01:00Z', 'ok', 0, repeat('a', 64))"
)
STATE = (
    "SELECT (SELECT count(*) FROM public.eye_schema_migrations), "
    "(SELECT count(*) FROM eye.capture_batch), (SELECT count(*) FROM eye.derivation_manifest), "
    "(SELECT count(*) FROM eye.derivation_run), (SELECT count(*) FROM eye.backtest_run), "
    "(SELECT string_agg(kind, ',') FROM eye.database_identity)"
)


@pytest.fixture
def config_path(example_raw, write_config):
    def _path(mode: str = "demo") -> str:
        raw = copy.deepcopy(example_raw)
        raw["server"]["port"] = 0
        if mode == "production":
            raw["runtime"]["mode"] = "production"
            raw["auth"] = {"adapter": "private", "private_adapter_module": "eye_test_private_auth"}
        written = write_config(raw)  # always the same file name
        path = written.with_name(f"eye-{mode}.toml")
        written.replace(path)
        return str(path)

    return _path


def run_eye(*args: str, config: str, url: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, EYE_DATABASE_URL=url)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "backend"), str(REPO_ROOT / "tests" / "support")]
    )
    env.pop("EYE_CONTAINER", None)
    env.pop("EYE_BACKUP_DIR", None)
    return subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "eye", *args, "--config", config],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=REPO_ROOT,
    )


def test_demo_refuses_a_private_database_before_serving_or_writing(make_db, config_path):
    demo, production = config_path(), config_path("production")
    url = make_db()
    # The private installation claims its database (db-migrate in production mode).
    claimed = run_eye("db-migrate", config=production, url=url)
    assert claimed.returncode == 0, claimed.stderr
    assert '"database": "private"' in claimed.stdout
    conn = connect(url)
    before = conn.run(STATE)[0]
    assert before[1:] == [0, 0, 0, 0, "private"]
    for args in DEMO_WRITERS:
        refused = run_eye(*args, config=demo, url=url)
        assert refused.returncode == 2, (args, refused.stdout, refused.stderr)
        assert "claimed by a private installation" in refused.stderr, args
    served = run_eye("serve", config=demo, url=url)  # would listen if not refused
    assert served.returncode == 4 and "claimed by a private installation" in served.stderr
    with pytest.raises(StartupError, match="private installation"):
        check_database(load_config(demo), url)
    assert conn.run(STATE)[0] == before  # nothing was migrated, written or recorded
    # Positive control: production accepts its own database.
    check_database(load_config(production), url)
    conn.close()


def test_demo_refuses_an_unclaimed_database_holding_a_real_source(make_db, config_path):
    demo = config_path()
    url = make_db()
    conn = connect(url)
    migrate(conn)  # migrated before any claim, as an older release would leave it
    with transaction(conn):
        conn.run(REAL_BATCH)
    before = conn.run(STATE)[0]
    assert before[-1] is None  # not claimed
    prepared = run_eye("db-prepare-demo", config=demo, url=url)  # the claiming command
    assert prepared.returncode == 2
    assert "non-synthetic sources (real-provider)" in prepared.stderr
    loaded = run_eye("db-load-fixtures", config=demo, url=url)  # needs an existing claim
    assert loaded.returncode == 2 and "claimed by no installation" in loaded.stderr
    with pytest.raises(StartupError, match="claimed by no installation"):
        check_database(load_config(demo), url)
    with pytest.raises(identity.IdentityRefused, match="non-synthetic"):
        identity.claim(conn, identity.DEMO)
    assert conn.run(STATE)[0] == before
    conn.close()


def test_demo_database_is_prepared_and_served_and_production_refuses_it(make_db, config_path):
    """Positive control for the demo, and the same boundary from the other side."""
    demo, production = config_path(), config_path("production")
    url = make_db()
    prepared = run_eye("db-prepare-demo", config=demo, url=url)
    assert prepared.returncode == 0, prepared.stderr
    again = run_eye("db-prepare-demo", config=demo, url=url)
    assert again.returncode == 0, again.stderr  # repeatable
    conn = connect(url)
    assert identity.kind(conn) == identity.DEMO
    check_database(load_config(demo), url)  # demo startup accepts it
    with pytest.raises(StartupError, match="claimed by the synthetic demo"):
        check_database(load_config(production), url)
    migrated = run_eye("db-migrate", config=production, url=url)
    assert migrated.returncode == 2 and "claimed by the synthetic demo" in migrated.stderr
    assert identity.kind(conn) == identity.DEMO
    # The claim is permanent: the table refuses a change or removal.
    for sql in (
        "UPDATE eye.database_identity SET kind = 'private'",
        "DELETE FROM eye.database_identity",
    ):
        with pytest.raises(DatabaseError), transaction(conn):
            conn.run(sql)
    assert identity.kind(conn) == identity.DEMO
    conn.close()


def test_production_refuses_an_unclaimed_database(make_db, config_path):
    url = make_db()
    conn = connect(url)
    migrate(conn)
    with pytest.raises(StartupError, match="claimed by no installation"):
        check_database(load_config(config_path("production")), url)
    identity.claim(conn, identity.PRIVATE)
    check_database(load_config(config_path("production")), url)  # positive control
    conn.close()


# --- O71: an unclaimed database ----------------------------------------------------


def test_demo_needs_its_own_claim_before_serving_or_writing(make_db, config_path):
    """O71: an unclaimed, migrated database is neither served nor written by the demo,
    so production can still claim it, and nothing synthetic lands in it."""
    demo, production = config_path(), config_path("production")
    url = make_db()
    conn = connect(url)
    migrate(conn)  # migrated, never claimed
    before = conn.run(STATE)[0]
    with pytest.raises(StartupError, match="claimed by no installation"):
        check_database(load_config(demo), url)
    for args in DEMO_WRITERS[2:]:  # every writer except the two that claim
        refused = run_eye(*args, config=demo, url=url)
        assert refused.returncode == 2, (args, refused.stdout, refused.stderr)
        assert "claimed by no installation" in refused.stderr, args
    assert conn.run(STATE)[0] == before  # no synthetic batch written, no claim
    # Positive control: production claims it and accepts it.
    claimed = run_eye("db-migrate", config=production, url=url)
    assert claimed.returncode == 0 and '"database": "private"' in claimed.stdout
    check_database(load_config(production), url)
    conn.close()


def test_production_refuses_an_unclaimed_database_the_demo_has_written(make_db, config_path):
    """A database an older release loaded with synthetic data stays out of production;
    the demo may claim it (positive control)."""
    demo, production = config_path(), config_path("production")
    url = make_db()
    conn = connect(url)
    migrate(conn)
    load_fixtures(conn, CAPTURES)  # synthetic batches, no claim
    before = conn.run(STATE)[0]
    refused = run_eye("db-migrate", config=production, url=url)
    assert refused.returncode == 2 and "synthetic sources" in refused.stderr
    with pytest.raises(identity.IdentityRefused, match="synthetic sources"):
        identity.claim(conn, identity.PRIVATE)
    assert conn.run(STATE)[0] == before
    prepared = run_eye("db-prepare-demo", config=demo, url=url)
    assert prepared.returncode == 0, prepared.stderr
    assert identity.kind(conn) == identity.DEMO
    check_database(load_config(demo), url)
    conn.close()


def test_a_running_demo_server_keeps_a_database_production_cannot_claim(make_db, config_path):
    """O71: the claim a demo server needs is permanent, so production cannot claim the
    database underneath it; the server keeps serving the synthetic demo."""
    import threading
    import urllib.request

    from eye.api.auth import DemoAuth
    from eye.api.server import build_server

    demo, production = config_path(), config_path("production")
    url = make_db()
    assert run_eye("db-prepare-demo", config=demo, url=url).returncode == 0
    server = build_server(load_config(demo), DemoAuth("demo"), url)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        refused = run_eye("db-migrate", config=production, url=url)
        assert refused.returncode == 2 and "claimed by the synthetic demo" in refused.stderr
        for args in (("db-replay",), ("db-rollup",)):
            other = run_eye(*args, config=production, url=url)
            assert other.returncode == 2 and "claimed by the synthetic demo" in other.stderr
        assert identity.kind(connect(url)) == identity.DEMO
        with urllib.request.urlopen(f"http://{host}:{port}/api/v0/health", timeout=10) as r:
            assert r.status == 200  # still the synthetic demo it started on
    finally:
        server.shutdown()
        server.server_close()


# --- O72: db-status needs no claim, so it must not write ----------------------------

# Every relation and schema outside the system catalogs, and the bookkeeping table.
CATALOG = (
    "SELECT to_regclass('public.eye_schema_migrations') IS NOT NULL, "
    "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')), "
    "(SELECT count(*) FROM pg_namespace)"
)
ALL = [m.version for m in discover()]


def test_status_leaves_a_fresh_unclaimed_database_unchanged(make_db, config_path):
    """O72: db-status runs without a claim, so it reads only; claimed databases report
    their migrations as before (positive control)."""
    demo, production = config_path(), config_path("production")
    for config in (demo, production):
        url = make_db()
        conn = connect(url)
        before = conn.run(CATALOG)[0]
        assert before[0] is False
        shown = run_eye("db-status", config=config, url=url)
        assert shown.returncode == 0, shown.stderr
        assert json.loads(shown.stdout) == {"applied": [], "pending": ALL}
        assert conn.run(CATALOG)[0] == before  # no bookkeeping table, nothing created
        conn.close()
    # Positive controls: a claimed database of each kind reports every migration applied.
    url = make_db()
    assert run_eye("db-prepare-demo", config=demo, url=url).returncode == 0
    shown = run_eye("db-status", config=demo, url=url)
    assert json.loads(shown.stdout) == {"applied": ALL, "pending": []}
    url = make_db()
    assert run_eye("db-migrate", config=production, url=url).returncode == 0
    shown = run_eye("db-status", config=production, url=url)
    assert json.loads(shown.stdout) == {"applied": ALL, "pending": []}


def test_status_works_for_a_role_that_cannot_create(make_db, config_path):
    """A separate mechanism for O72: a login role without CREATE on the database or
    its public schema can run db-status on a fresh database, and the server refuses
    that role a write (negative control for the role itself)."""
    import secrets
    import uuid
    from urllib.parse import quote, urlsplit, urlunsplit

    url = make_db()
    role, password = f"eye_t_reader_{uuid.uuid4().hex[:8]}", secrets.token_hex(16)
    admin = connect(url)
    admin.run(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    try:
        admin.run(f"REVOKE CREATE ON SCHEMA public FROM PUBLIC, {role}")
        parts = urlsplit(url)
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        reader = urlunsplit(parts._replace(netloc=f"{role}:{quote(password)}@{host}"))
        shown = run_eye("db-status", config=config_path(), url=reader)
        assert shown.returncode == 0, shown.stderr
        assert json.loads(shown.stdout) == {"applied": [], "pending": ALL}
        conn = connect(reader)
        with pytest.raises(DatabaseError, match="permission denied"):
            conn.run("CREATE TABLE public.eye_schema_migrations (version integer)")
        conn.close()
    finally:
        admin.run(f"DROP OWNED BY {role}")
        admin.run(f"DROP ROLE {role}")
        admin.close()
