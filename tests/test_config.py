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
    assert config.capture_fixtures.is_dir() and config.ais_capture_fixtures.is_dir()
    assert config.api.ws_max_buffer_bytes >= config.server.max_response_bytes
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


def test_client_buffer_must_hold_one_full_message(example_raw):
    example_raw["api"]["ws_max_buffer_bytes"] = example_raw["server"]["max_response_bytes"] - 1
    with pytest.raises(ConfigError, match="ws_max_buffer_bytes"):
        parse(example_raw)
    example_raw["api"]["ws_max_buffer_bytes"] = example_raw["server"]["max_response_bytes"]
    assert parse(example_raw).api.ws_max_buffer_bytes == example_raw["server"]["max_response_bytes"]


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("api", "max_websockets", 0),
        ("api", "poll_interval_ms", 10),
        ("api", "max_counts", 169),
        ("api", "allowed_origin", "javascript:alert(1)"),
        ("view", "bbox", [1, 0, 0, 1]),
        ("view", "layers", ["ships"]),
        ("api", "unknown_limit", 1),
    ],
)
def test_api_and_view_limits_are_checked(example_raw, section, key, value):
    bad = copy.deepcopy(example_raw)
    bad[section][key] = value
    with pytest.raises(ConfigError):
        parse(bad)
    assert parse(example_raw).api.max_websockets == 8  # control


def test_allowed_origin_accepts_an_exact_origin(example_raw):
    example_raw["api"]["allowed_origin"] = "https://eye.example.org"
    assert parse(example_raw).api.allowed_origin == "https://eye.example.org"


# --- Package 5 retention settings ---------------------------------------------------


def test_retention_deletion_is_off_by_default(example_raw):
    config = parse(example_raw)
    assert config.retention.allow_deletion is False
    assert config.retention.lateness_hours == 48
    assert config.retention.backup_dir_env == "EYE_BACKUP_DIR"
    del example_raw["retention"]
    assert parse(example_raw).retention.allow_deletion is False  # absent table: still off


def test_retention_deletion_is_refused_outside_demo(example_raw):
    example_raw["retention"]["allow_deletion"] = True
    assert parse(example_raw).retention.allow_deletion is True  # control: demo may enable it
    raw = production(example_raw, "eye_test_private_auth")
    with pytest.raises(ConfigError, match="refused outside demo mode"):
        parse(raw)
    raw["retention"]["allow_deletion"] = False
    assert parse(raw).retention.allow_deletion is False  # control: production, deletion off


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("allow_deletion", "yes", "true or false"),
        ("lateness_hours", 0, "lateness_hours"),
        ("backup_dir_env", "/srv/backup", "environment variable name"),
        ("restore_url_env", "EYE_DATABASE_URL", "other than"),
        ("unknown", 1, "unknown key"),
    ],
)
def test_retention_settings_are_validated(example_raw, key, value, message):
    assert parse(example_raw)  # control
    example_raw["retention"][key] = value
    with pytest.raises(ConfigError, match=message):
        parse(example_raw)
