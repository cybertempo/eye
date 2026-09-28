"""Start the real demo process on an ephemeral loopback port and exercise it."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys

import pytest
from conftest import REPO_ROOT


@pytest.fixture
def demo(example_raw, write_config):
    example_raw["server"]["port"] = 0
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "backend"))
    env.pop("EYE_CONTAINER", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "eye", "serve", "--config", str(write_config(example_raw))],
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
        yield match.group(1), int(match.group(2))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def request(addr, path, method="GET", host=None):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    headers = {"Host": host} if host else {}
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def test_health_and_snapshot(demo):
    response, body = request(demo, "/api/v0/health")
    assert response.status == 200
    assert json.loads(body) == {"mode": "demo", "status": "ok", "synthetic": True}

    response, body = request(demo, "/api/v0/snapshot")
    assert response.status == 200
    snapshot = json.loads(body)
    assert snapshot["synthetic"] is True
    assert {t["kind"] for t in snapshot["tracks"]} == {"flight", "vessel"}


def test_security_headers(demo):
    response, _ = request(demo, "/api/v0/health")
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert "default-src 'none'" in response.getheader("Content-Security-Policy")
    assert response.getheader("Cache-Control") == "no-store"


def test_foreign_host_header_refused_loopback_accepted(demo):
    ok, _ = request(demo, "/api/v0/health", host=f"localhost:{demo[1]}")
    assert ok.status == 200
    refused, _ = request(demo, "/api/v0/health", host="eye.example.org")
    assert refused.status == 421


def test_write_methods_refused(demo):
    ok, _ = request(demo, "/api/v0/snapshot", method="HEAD")
    assert ok.status == 200
    refused, _ = request(demo, "/api/v0/snapshot", method="POST")
    assert refused.status == 405


def test_demo_page_served(demo):
    response, body = request(demo, "/")
    assert response.status == 200
    assert b"SYNTHETIC DATA" in body
    missing, _ = request(demo, "/../config/eye.example.toml")
    assert missing.status == 404
