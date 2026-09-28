"""Deployment and CI files stay pinned and loopback-only."""

from __future__ import annotations

import re
import tomllib

import pytest
from conftest import REPO_ROOT
from eye.config import ConfigError, load_config

COMPOSE = REPO_ROOT / "deploy" / "dev" / "compose.yaml"
CONTAINER_CONFIG = REPO_ROOT / "deploy" / "dev" / "eye.container.toml"
DOCKERFILE = REPO_ROOT / "deploy" / "dev" / "Dockerfile"
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))


def test_compose_publishes_on_loopback_only():
    ports = re.findall(r'^\s*-\s*"([^"]+)"\s*$', COMPOSE.read_text(), re.MULTILINE)
    assert ports, "compose must publish the demo port"
    for mapping in ports:
        assert mapping.startswith("127.0.0.1:"), mapping
    container_port = tomllib.loads(CONTAINER_CONFIG.read_text())["server"]["port"]
    assert all(m.endswith(f":{container_port}") for m in ports)
    assert all(f":-{container_port}}}" in m for m in ports)


def test_container_config_needs_container_env():
    assert load_config(CONTAINER_CONFIG, environ={"EYE_CONTAINER": "1"}).mode == "demo"
    with pytest.raises(ConfigError):
        load_config(CONTAINER_CONFIG, environ={})


def test_base_images_pinned_by_digest():
    froms = re.findall(r"^FROM\s+(\S+)", DOCKERFILE.read_text(), re.MULTILINE)
    assert froms
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in froms), froms


def test_workflow_actions_pinned_by_commit():
    assert WORKFLOWS
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        for ref in re.findall(r"uses:\s*(\S+)", text):
            if ref.startswith("./"):
                continue
            if ref.startswith("docker://"):
                assert re.search(r"@sha256:[0-9a-f]{64}$", ref), ref
            else:
                assert re.search(r"@[0-9a-f]{40}$", ref), ref
        for image in re.findall(r"docker run[^\n]*?\s(\S+@sha256:\S+)", text):
            assert re.search(r"@sha256:[0-9a-f]{64}$", image), image
