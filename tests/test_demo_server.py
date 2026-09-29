"""Start the real `serve` process on an ephemeral loopback port and exercise it."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys

import pytest
from conftest import REPO_ROOT
from eye.api import feed
from eye.api.server import MODULES, STATIC_FILES
from eye.wire import validate_message


def run_eye(*args, url: str, timeout: float = 120):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "backend"), EYE_DATABASE_URL=url)
    env.pop("EYE_CONTAINER", None)
    return subprocess.run(
        [sys.executable, "-m", "eye", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=timeout,
    )


@pytest.fixture
def demo(make_db, example_raw, write_config):
    url = make_db()
    config = str(write_config({**example_raw, "server": {**example_raw["server"], "port": 0}}))
    prepared = run_eye("db-prepare-demo", "--config", config, url=url)
    assert prepared.returncode == 0, prepared.stderr
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "backend"), EYE_DATABASE_URL=url)
    env.pop("EYE_CONTAINER", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "eye", "serve", "--config", config],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )
    try:
        line = proc.stdout.readline()
        match = re.search(r"http://(127\.0\.0\.1):(\d+)/", line)
        assert match, f"demo did not start: {line!r} {proc.stderr.read() if proc.poll() else ''}"
        assert "open it yourself" in line
        yield match.group(1), int(match.group(2))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def request(addr, path, method="GET", host=None):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    headers = {"Host": host} if host else {}
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def test_health_snapshot_and_transits(demo):
    response, body = request(demo, "/api/v0/health")
    assert response.status == 200
    assert json.loads(body) == {
        "schema_version": "eye.wire/2",
        "kind": "health",
        "mode": "demo",
        "status": "ok",
        "synthetic": True,
    }
    response, body = request(demo, "/api/v0/snapshot")
    assert response.status == 200
    snapshot = validate_message(body, "ServerMessage")
    assert snapshot["kind"] == "snapshot" and snapshot["synthetic"] is True
    assert {t["kind"] for t in snapshot["tracks"]} == {"vessel"}
    response, body = request(
        demo, "/api/v0/snapshot?start=2026-01-01T00:00:00Z&end=2026-01-01T02:00:00Z"
    )
    assert {t["kind"] for t in validate_message(body, "ServerMessage")["tracks"]} == {
        "flight",
        "vessel",
    }
    response, body = request(demo, "/api/v0/transits")
    assert response.status == 200
    assert validate_message(body, "ServerMessage")["kind"] == "transits"


def test_security_headers(demo):
    for path in ("/api/v0/health", "/", "/static/app.js"):
        response, _ = request(demo, path)
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        csp = response.getheader("Content-Security-Policy")
        assert "default-src 'none'" in csp and "script-src 'self'" in csp
        assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert response.getheader("Cache-Control") == "no-store"


def test_foreign_host_header_refused_loopback_accepted(demo):
    ok, _ = request(demo, "/api/v0/health", host=f"localhost:{demo[1]}")
    assert ok.status == 200
    for path in ("/api/v0/health", "/api/v0/transits", "/"):
        refused, body = request(demo, path, host="eye.example.org")
        assert refused.status == 421
        assert validate_message(body, "ServerMessage")["kind"] == "error"


def test_write_methods_refused(demo):
    ok, _ = request(demo, "/api/v0/snapshot", method="HEAD")
    assert ok.status == 200
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        refused, _ = request(demo, "/api/v0/snapshot", method=method)
        assert refused.status == 405


def test_browser_modules_served(demo):
    for module in MODULES:
        response, _ = request(demo, f"/static/{module}.js")
        assert response.status == 200, module
        assert response.getheader("Content-Type").startswith("text/javascript")
    for missing in ("/static/../config/eye.example.toml", "/static/demo.js", "/static/app.ts"):
        assert request(demo, missing)[0].status == 404


def test_demo_page_served(demo):
    response, body = request(demo, "/")
    assert response.status == 200
    assert b"SYNTHETIC DATA" in body and b"THEATRE" in body and b"DESK" in body
    missing, _ = request(demo, "/../config/eye.example.toml")
    assert missing.status == 404


def test_response_limit_applies_to_static_files(api_server, tmp_path, monkeypatch):
    api = api_server(server={"max_response_bytes": 4096}, api={"ws_max_buffer_bytes": 16384})
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * 4097)
    monkeypatch.setitem(STATIC_FILES, "/static/oversized.txt", (oversized, "text/plain"))
    permitted, _ = api.get("/static/app.css")
    assert permitted.status == 200
    refused, body = api.get("/static/oversized.txt")
    assert refused.status == 503 and len(body) <= 4096
    assert validate_message(body, "ServerMessage")["kind"] == "error"


def test_an_outgoing_message_that_breaks_the_schema_is_never_sent(api_server, monkeypatch):
    api = api_server()
    assert api.get("/api/v0/snapshot")[0].status == 200  # control
    real = feed.snapshot

    def outage_as_zero(*args, **kwargs):
        message = real(*args, **kwargs)
        failed = next(c for c in message["coverage"] if c["state"] == "failed")
        failed["metric"]["value"] = 0
        return message

    monkeypatch.setattr(feed, "snapshot", outage_as_zero)
    response, body = api.get("/api/v0/snapshot")
    assert response.status == 500
    assert json.loads(body) == {
        "schema_version": "eye.wire/2",
        "kind": "error",
        "status": 500,
        "error": "internal error: invalid outgoing message",
    }


def test_serve_refuses_an_unreachable_or_unmigrated_database(make_db, example_raw, write_config):
    config = str(write_config({**example_raw, "server": {**example_raw["server"], "port": 0}}))
    unreachable = run_eye(
        "serve", "--config", config, url="postgresql://eye:x@127.0.0.1:1/eye", timeout=60
    )
    assert unreachable.returncode == 4 and "database unreachable" in unreachable.stderr
    empty = make_db()
    unmigrated = run_eye("serve", "--config", config, url=empty, timeout=60)
    assert unmigrated.returncode == 4 and "not migrated" in unmigrated.stderr
    missing = subprocess.run(
        [sys.executable, "-m", "eye", "serve", "--config", config],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(REPO_ROOT / "backend")},
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert missing.returncode == 2 and "EYE_DATABASE_URL is not set" in missing.stderr
    # Control: the same configuration starts once the database is migrated.
    assert run_eye("db-migrate", "--config", config, url=empty).returncode == 0


def test_demo_refuses_a_non_loopback_database(example_raw, write_config):
    config = str(write_config(example_raw))
    result = run_eye("serve", "--config", config, url="postgresql://eye:x@db.example.org/eye")
    assert result.returncode == 4 and "only to a loopback database" in result.stderr
