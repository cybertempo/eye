"""Load and validate EYE configuration.

All settings come from one TOML file (see ``config/eye.example.toml``). The loader
rejects unknown keys and any combination that would weaken the public/private
boundary, so an unsafe file stops the process before a socket is opened.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_SCHEMA_VERSION = 1
MODES = ("demo", "production")
AUTH_ADAPTERS = ("demo", "private")
# Set by deploy/dev/Dockerfile. Only then may the demo bind every interface
# inside its own network namespace; compose publishes the port on loopback.
CONTAINER_ENV_VAR = "EYE_CONTAINER"

_ALLOWED_KEYS: dict[str, set[str]] = {
    "": {"schema_version", "runtime", "server", "auth", "data", "database", "providers"},
    "runtime": {"mode"},
    "server": {
        "bind_host",
        "port",
        "container_internal_bind",
        "max_response_bytes",
        "request_timeout_seconds",
        "max_connections",
    },
    "auth": {"adapter", "private_adapter_module"},
    "data": {"fixture", "capture_fixtures"},
    "database": {"url_env"},
    "providers": {"enabled"},
}


class ConfigError(ValueError):
    """The configuration is invalid or unsafe; the process must not start."""


@dataclass(frozen=True)
class ServerConfig:
    bind_host: str
    port: int
    container_internal_bind: bool
    max_response_bytes: int
    request_timeout_seconds: float
    max_connections: int


@dataclass(frozen=True)
class AuthConfig:
    adapter: str
    private_adapter_module: str


@dataclass(frozen=True)
class EyeConfig:
    source: Path
    mode: str
    server: ServerConfig
    auth: AuthConfig
    fixture: Path
    capture_fixtures: Path | None
    database_url_env: str
    enabled_providers: tuple[str, ...]


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_keys(section: str, table: dict) -> None:
    unknown = set(table) - _ALLOWED_KEYS[section]
    if unknown:
        where = f"[{section}]" if section else "top level"
        raise ConfigError(f"unknown key(s) at {where}: {', '.join(sorted(unknown))}")


def _table(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    _check_keys(name, value)
    return value


def _int(table: dict, key: str, default: int, low: int, high: int) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"{key} must be an integer from {low} to {high}")
    return value


def parse_config(raw: dict, source: Path, environ: dict[str, str] | None = None) -> EyeConfig:
    """Validate a parsed TOML document. Raises ConfigError on any unsafe setting."""
    env = os.environ if environ is None else environ
    _check_keys("", raw)
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {CONFIG_SCHEMA_VERSION}")

    mode = _table(raw, "runtime").get("mode", "demo")
    if mode not in MODES:
        raise ConfigError(f"runtime.mode must be one of {MODES}")

    srv = _table(raw, "server")
    bind_host = srv.get("bind_host", "127.0.0.1")
    if not isinstance(bind_host, str) or not bind_host:
        raise ConfigError("server.bind_host must be a non-empty string")
    container_bind = srv.get("container_internal_bind", False)
    if not isinstance(container_bind, bool):
        raise ConfigError("server.container_internal_bind must be true or false")
    server = ServerConfig(
        bind_host=bind_host,
        port=_int(srv, "port", 0, 0, 65535),
        container_internal_bind=container_bind,
        max_response_bytes=_int(srv, "max_response_bytes", 1_048_576, 1024, 16_777_216),
        request_timeout_seconds=float(_int(srv, "request_timeout_seconds", 10, 1, 120)),
        max_connections=_int(srv, "max_connections", 16, 1, 256),
    )

    auth_raw = _table(raw, "auth")
    auth = AuthConfig(
        adapter=auth_raw.get("adapter", "demo"),
        private_adapter_module=auth_raw.get("private_adapter_module", ""),
    )
    if auth.adapter not in AUTH_ADAPTERS:
        raise ConfigError(f"auth.adapter must be one of {AUTH_ADAPTERS}")
    if not isinstance(auth.private_adapter_module, str):
        raise ConfigError("auth.private_adapter_module must be a string")

    data = _table(raw, "data")
    fixture_value = data.get("fixture", "")
    if not isinstance(fixture_value, str) or not fixture_value:
        raise ConfigError("data.fixture must name a synthetic fixture file")
    fixture = Path(fixture_value)
    if not fixture.is_absolute():
        fixture = (source.parent / fixture).resolve()
    captures_value = data.get("capture_fixtures")
    capture_fixtures = None
    if captures_value is not None:
        if not isinstance(captures_value, str) or not captures_value:
            raise ConfigError("data.capture_fixtures must name a directory")
        capture_fixtures = Path(captures_value)
        if not capture_fixtures.is_absolute():
            capture_fixtures = (source.parent / capture_fixtures).resolve()

    # The URL itself (with any password) lives only in the environment or a
    # private runtime secret; configuration names the variable that holds it.
    url_env = _table(raw, "database").get("url_env", "EYE_DATABASE_URL")
    if not isinstance(url_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", url_env):
        raise ConfigError("database.url_env must be an environment variable name")

    enabled = _table(raw, "providers").get("enabled", [])
    if not isinstance(enabled, list) or not all(isinstance(p, str) for p in enabled):
        raise ConfigError("providers.enabled must be a list of names")
    if enabled:
        # No provider adapter exists yet and none has an approved row in
        # docs/source-policy-register.md, so any entry is refused.
        raise ConfigError(
            "real provider adapters are disabled until approved in "
            f"docs/source-policy-register.md; refusing: {', '.join(enabled)}"
        )

    if mode == "demo":
        if auth.adapter != "demo":
            raise ConfigError("demo mode uses the demo auth adapter only")
        if auth.private_adapter_module:
            raise ConfigError("demo mode must not name a private auth adapter")
        if not is_loopback_host(bind_host):
            allowed_in_container = (
                container_bind
                and bind_host == "0.0.0.0"  # noqa: S104 - checked, container only
                and env.get(CONTAINER_ENV_VAR) == "1"
            )
            if not allowed_in_container:
                raise ConfigError(
                    f"demo mode must bind to loopback, not {bind_host!r} "
                    f"(0.0.0.0 is allowed only inside the dev container with "
                    f"server.container_internal_bind = true)"
                )
    else:
        if auth.adapter != "private":
            raise ConfigError(
                "production mode requires auth.adapter = 'private'; "
                "demo authentication is never a production fallback"
            )
        if not auth.private_adapter_module:
            raise ConfigError(
                "production mode requires auth.private_adapter_module; "
                "the private authentication adapter is absent"
            )
        if container_bind:
            raise ConfigError("server.container_internal_bind is a demo-only setting")
        if not is_loopback_host(bind_host):
            raise ConfigError("the EYE backend binds to loopback; the private gateway fronts it")

    return EyeConfig(
        source=source,
        mode=mode,
        server=server,
        auth=auth,
        fixture=fixture,
        capture_fixtures=capture_fixtures,
        database_url_env=url_env,
        enabled_providers=tuple(enabled),
    )


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> EyeConfig:
    source = Path(path).resolve()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {source}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration is not valid TOML: {exc}") from exc
    return parse_config(raw, source, environ)
