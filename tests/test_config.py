"""Configuration refusals, each beside a successful control."""

from __future__ import annotations

import copy

import pytest
from conftest import EXAMPLE_CONFIG
from eye.api.auth import AuthUnavailable, DemoAuth, resolve_auth
from eye.config import ConfigError, load_config, parse_config


def parse(raw: dict, environ: dict[str, str] | None = None):
    return parse_config(raw, EXAMPLE_CONFIG, environ or {})


def production(raw: dict, module: str) -> dict:
    raw = copy.deepcopy(raw)
    raw["runtime"]["mode"] = "production"
    raw["auth"]["adapter"] = "private"
    raw["auth"]["private_adapter_module"] = module
    return raw


def test_example_config_is_safe_demo():
    config = load_config(EXAMPLE_CONFIG, environ={})
    assert config.mode == "demo"
    assert config.server.bind_host == "127.0.0.1"
    assert config.enabled_providers == ()
    assert config.fixture.is_file()
    assert isinstance(resolve_auth(config), DemoAuth)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_hosts_accepted(example_raw, host):
    example_raw["server"]["bind_host"] = host
    assert parse(example_raw).server.bind_host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "eye.example.org"])
def test_non_loopback_bind_refused_in_demo(example_raw, host):
    example_raw["server"]["bind_host"] = host
    with pytest.raises(ConfigError, match="loopback"):
        parse(example_raw)


def test_container_bind_needs_flag_and_container_env(example_raw):
    example_raw["server"]["bind_host"] = "0.0.0.0"
    example_raw["server"]["container_internal_bind"] = True
    assert parse(example_raw, {"EYE_CONTAINER": "1"}).server.bind_host == "0.0.0.0"
    with pytest.raises(ConfigError, match="loopback"):
        parse(example_raw, {})
    example_raw["server"]["container_internal_bind"] = False
    with pytest.raises(ConfigError, match="loopback"):
        parse(example_raw, {"EYE_CONTAINER": "1"})


def test_provider_enable_refused_and_empty_accepted(example_raw):
    assert parse(example_raw).enabled_providers == ()
    example_raw["providers"]["enabled"] = ["opensky"]
    with pytest.raises(ConfigError, match="source-policy-register"):
        parse(example_raw)


def test_unknown_key_refused(example_raw):
    parse(example_raw)
    example_raw["server"]["api_token"] = "x"
    with pytest.raises(ConfigError, match="unknown key"):
        parse(example_raw)


def test_demo_mode_refuses_private_adapter_settings(example_raw):
    example_raw["auth"]["adapter"] = "private"
    with pytest.raises(ConfigError, match="demo auth"):
        parse(example_raw)


def test_production_without_private_adapter_refused(example_raw):
    raw = production(example_raw, "")
    with pytest.raises(ConfigError, match="adapter is absent"):
        parse(raw)


def test_production_with_demo_adapter_refused(example_raw):
    raw = production(example_raw, "eye_test_private_auth")
    raw["auth"]["adapter"] = "demo"
    with pytest.raises(ConfigError, match="never a production fallback"):
        parse(raw)


def test_production_with_private_adapter_accepted(example_raw):
    """Positive control: a present private adapter satisfies the auth gate."""
    config = parse(production(example_raw, "eye_test_private_auth"))
    adapter = resolve_auth(config)
    assert adapter.name == "test-private"
    assert not isinstance(adapter, DemoAuth)
    assert adapter.authenticate({}) is None


def test_production_with_uninstalled_adapter_refused(example_raw):
    config = parse(production(example_raw, "eye_private_adapter_that_is_not_installed"))
    with pytest.raises(AuthUnavailable, match="not installed"):
        resolve_auth(config)


def test_production_adapter_returning_demo_auth_refused(example_raw):
    config = parse(production(example_raw, "eye_test_bad_auth"))
    with pytest.raises(AuthUnavailable, match="did not return"):
        resolve_auth(config)


def test_demo_auth_refuses_production_mode():
    assert DemoAuth("demo").authenticate({}).synthetic is True
    with pytest.raises(AuthUnavailable):
        DemoAuth("production")


def test_production_must_bind_loopback(example_raw):
    raw = production(example_raw, "eye_test_private_auth")
    raw["server"]["bind_host"] = "0.0.0.0"
    with pytest.raises(ConfigError, match="loopback"):
        parse(raw)
