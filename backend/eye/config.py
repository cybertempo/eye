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
    "": {
        "schema_version",
        "runtime",
        "server",
        "api",
        "view",
        "auth",
        "data",
        "database",
        "providers",
        "retention",
    },
    "runtime": {"mode"},
    "server": {
        "bind_host",
        "port",
        "container_internal_bind",
        "max_response_bytes",
        "request_timeout_seconds",
        "max_connections",
    },
    "api": {
        "allowed_origin",
        "max_websockets",
        "ws_max_buffer_bytes",
        "ws_max_inbound_bytes",
        "ws_send_timeout_seconds",
        "ws_idle_timeout_seconds",
        "ws_ping_seconds",
        "poll_interval_ms",
        "db_pool_size",
        "query_timeout_ms",
        "max_interval_hours",
        "max_tracks",
        "max_points",
        "max_coverage",
        "max_counts",
        "max_crossings",
        "max_events",
        "max_media",
        "max_pending_changes",
        "default_view_hours",
    },
    "view": {"area_name", "bbox", "layers"},
    "auth": {"adapter", "private_adapter_module"},
    "data": {
        "capture_fixtures",
        "ais_capture_fixtures",
        "event_capture_fixtures",
        "media_capture_fixtures",
        "count_lines",
    },
    "database": {"url_env"},
    "providers": {"enabled"},
    "retention": {"allow_deletion", "lateness_hours", "backup_dir_env", "restore_url_env"},
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
class ApiConfig:
    """Bounds for the browser API. Every queue, query and buffer has a limit."""

    allowed_origin: str
    max_websockets: int
    ws_max_buffer_bytes: int
    ws_max_inbound_bytes: int
    ws_send_timeout_seconds: int
    ws_idle_timeout_seconds: int
    ws_ping_seconds: int
    poll_interval_ms: int
    db_pool_size: int
    query_timeout_ms: int
    max_interval_hours: int
    max_tracks: int
    max_points: int
    max_coverage: int
    max_counts: int
    max_crossings: int
    max_events: int
    max_media: int
    max_pending_changes: int
    default_view_hours: int


@dataclass(frozen=True)
class ViewConfig:
    area_name: str
    bbox: tuple[float, float, float, float]
    layers: tuple[str, ...]


@dataclass(frozen=True)
class AuthConfig:
    adapter: str
    private_adapter_module: str


@dataclass(frozen=True)
class RetentionConfig:
    """Package 5 lifecycle settings. Deletion is off unless explicitly enabled."""

    allow_deletion: bool
    lateness_hours: int
    backup_dir_env: str
    restore_url_env: str


@dataclass(frozen=True)
class EyeConfig:
    source: Path
    mode: str
    server: ServerConfig
    api: ApiConfig
    view: ViewConfig
    auth: AuthConfig
    capture_fixtures: Path | None
    ais_capture_fixtures: Path | None
    event_capture_fixtures: Path | None
    media_capture_fixtures: Path | None
    count_lines: Path | None
    database_url_env: str
    enabled_providers: tuple[str, ...]
    retention: RetentionConfig


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


def _api(table: dict) -> ApiConfig:
    origin = table.get("allowed_origin", "")
    if not isinstance(origin, str) or (
        origin and not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]{1,253}", origin)
    ):
        raise ConfigError("api.allowed_origin must be empty or scheme://host[:port]")
    return ApiConfig(
        allowed_origin=origin,
        max_websockets=_int(table, "max_websockets", 8, 1, 64),
        ws_max_buffer_bytes=_int(table, "ws_max_buffer_bytes", 2_097_152, 16_384, 33_554_432),
        ws_max_inbound_bytes=_int(table, "ws_max_inbound_bytes", 16_384, 1024, 65_536),
        ws_send_timeout_seconds=_int(table, "ws_send_timeout_seconds", 5, 1, 60),
        ws_idle_timeout_seconds=_int(table, "ws_idle_timeout_seconds", 120, 10, 3600),
        ws_ping_seconds=_int(table, "ws_ping_seconds", 30, 5, 600),
        poll_interval_ms=_int(table, "poll_interval_ms", 1000, 50, 60_000),
        db_pool_size=_int(table, "db_pool_size", 4, 1, 32),
        query_timeout_ms=_int(table, "query_timeout_ms", 5000, 100, 60_000),
        max_interval_hours=_int(table, "max_interval_hours", 168, 1, 744),
        max_tracks=_int(table, "max_tracks", 1000, 1, 1000),
        max_points=_int(table, "max_points", 20_000, 1, 100_000),
        max_coverage=_int(table, "max_coverage", 1000, 1, 1000),
        max_counts=_int(table, "max_counts", 168, 1, 168),
        max_crossings=_int(table, "max_crossings", 1000, 1, 1000),
        max_events=_int(table, "max_events", 500, 1, 1000),
        max_media=_int(table, "max_media", 200, 1, 500),
        max_pending_changes=_int(table, "max_pending_changes", 50, 1, 1000),
        default_view_hours=_int(table, "default_view_hours", 4, 1, 168),
    )


def _view(table: dict) -> ViewConfig:
    name = table.get("area_name", "Synthetic test area near 0N 0E")
    if not isinstance(name, str) or not 1 <= len(name) <= 200:
        raise ConfigError("view.area_name must be 1 to 200 characters")
    bbox = table.get("bbox", [-0.5, -0.5, 0.5, 0.5])
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in bbox)
        or not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90)
    ):
        raise ConfigError("view.bbox must be [west, south, east, north] in degrees")
    layers = table.get("layers", ["flight", "vessel", "road"])
    if (
        not isinstance(layers, list)
        or not layers
        or len(set(layers)) != len(layers)
        or not all(layer in ("flight", "vessel", "road") for layer in layers)
    ):
        raise ConfigError("view.layers must be a non-empty list of flight, vessel, road")
    return ViewConfig(name, tuple(float(v) for v in bbox), tuple(layers))  # type: ignore[arg-type]


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

    api = _api(_table(raw, "api"))
    if api.ws_max_buffer_bytes < server.max_response_bytes:
        raise ConfigError(
            "api.ws_max_buffer_bytes must be at least server.max_response_bytes, "
            "so one full message fits in a client's buffer"
        )
    view = _view(_table(raw, "view"))

    data = _table(raw, "data")

    def directory(key: str) -> Path | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ConfigError(f"data.{key} must name a directory")
        path = Path(value)
        return path if path.is_absolute() else (source.parent / path).resolve()

    capture_fixtures = directory("capture_fixtures")
    ais_capture_fixtures = directory("ais_capture_fixtures")
    event_capture_fixtures = directory("event_capture_fixtures")
    media_capture_fixtures = directory("media_capture_fixtures")
    count_lines = directory("count_lines")

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

    ret = _table(raw, "retention")
    allow_deletion = ret.get("allow_deletion", False)
    if not isinstance(allow_deletion, bool):
        raise ConfigError("retention.allow_deletion must be true or false")
    backup_dir_env = ret.get("backup_dir_env", "EYE_BACKUP_DIR")
    if not isinstance(backup_dir_env, str) or not re.fullmatch(
        r"[A-Z][A-Z0-9_]{0,63}", backup_dir_env
    ):
        raise ConfigError("retention.backup_dir_env must be an environment variable name")
    restore_url_env = ret.get("restore_url_env", "EYE_RESTORE_DATABASE_URL")
    if (
        not isinstance(restore_url_env, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", restore_url_env)
        or restore_url_env == url_env
    ):
        raise ConfigError(
            "retention.restore_url_env must be an environment variable name other than "
            "database.url_env (a drill restores into a separate, empty database)"
        )
    retention = RetentionConfig(
        allow_deletion=allow_deletion,
        lateness_hours=_int(ret, "lateness_hours", 48, 1, 8760),
        backup_dir_env=backup_dir_env,
        restore_url_env=restore_url_env,
    )
    if allow_deletion and mode != "demo":
        # The only backup target in this repository is synthetic; production
        # retention needs the private, independently verified backup first.
        raise ConfigError(
            "retention.allow_deletion is refused outside demo mode: no verified "
            "private backup target exists in this repository"
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
        api=api,
        view=view,
        auth=auth,
        capture_fixtures=capture_fixtures,
        ais_capture_fixtures=ais_capture_fixtures,
        event_capture_fixtures=event_capture_fixtures,
        media_capture_fixtures=media_capture_fixtures,
        count_lines=count_lines,
        database_url_env=url_env,
        enabled_providers=tuple(enabled),
        retention=retention,
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
