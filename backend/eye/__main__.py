"""Command-line entry point: ``python -m eye COMMAND --config PATH``.

Commands: check-config, serve, db-migrate, db-status, db-load-fixtures,
db-replay. Exit codes: 0 success, 2 configuration refused, 3 authentication
adapter refused, 4 other startup refusal, 5 database operation failed.
Nothing here opens a browser.
"""

from __future__ import annotations

import argparse
import sys

from eye.api.auth import AuthUnavailable, resolve_auth
from eye.config import ConfigError, EyeConfig, load_config

EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_STARTUP = 4
EXIT_DATABASE = 5
DB_COMMANDS = ("db-migrate", "db-status", "db-load-fixtures", "db-replay")


def _prepare(path: str) -> tuple[EyeConfig, object]:
    config = load_config(path)
    return config, resolve_auth(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eye")
    parser.add_argument("command", choices=("check-config", "serve", *DB_COMMANDS))
    parser.add_argument("--config", required=True, help="path to an EYE TOML configuration")
    args = parser.parse_args(argv)

    try:
        config, auth = _prepare(args.config)
    except ConfigError as exc:
        print(f"eye: REFUSED (configuration): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AuthUnavailable as exc:
        print(f"eye: REFUSED (authentication): {exc}", file=sys.stderr)
        return EXIT_AUTH

    if args.command == "check-config":
        print(f"eye: configuration accepted (mode={config.mode}, auth={auth.name})")
        return 0

    if args.command in DB_COMMANDS:
        return _database_command(args.command, config)

    if config.mode != "demo":
        print(
            "eye: REFUSED (startup): production serving is not implemented in this build; "
            "the private integration is Package 8",
            file=sys.stderr,
        )
        return EXIT_STARTUP

    from eye.api.server import StartupError, build_server

    try:
        server = build_server(config, auth)
    except (StartupError, OSError) as exc:
        print(f"eye: REFUSED (startup): {exc}", file=sys.stderr)
        return EXIT_STARTUP

    host, port = server.server_address[:2]
    shown = f"[{host}]" if ":" in host else host
    print(f"eye: synthetic demo listening on http://{shown}:{port}/ (open it yourself)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _database_command(command: str, config: EyeConfig) -> int:
    import json
    import os

    from pg8000.exceptions import DatabaseError, InterfaceError

    from eye.ingest.capture import CaptureRejected, load_fixtures, replay_pending, verify_replay
    from eye.storage.db import DatabaseConfigError, connect
    from eye.storage.migrate import MigrationError, migrate, status

    url = os.environ.get(config.database_url_env)
    if not url:
        print(
            f"eye: REFUSED (configuration): environment variable {config.database_url_env} "
            "is not set",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    if command == "db-load-fixtures":
        if config.mode != "demo":
            print(
                "eye: REFUSED (configuration): synthetic fixtures load in demo mode only",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        if config.capture_fixtures is None:
            print("eye: REFUSED (configuration): data.capture_fixtures is not set", file=sys.stderr)
            return EXIT_CONFIG
    try:
        conn = connect(url, require_loopback=config.mode == "demo")
    except DatabaseConfigError as exc:
        print(f"eye: REFUSED (configuration): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # connection failures are reported, never treated as empty
        print(f"eye: UNVERIFIED (database unreachable): {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    try:
        if command == "db-migrate":
            result = {"applied_now": migrate(conn)}
        elif command == "db-status":
            result = status(conn)
        elif command == "db-load-fixtures":
            batches = load_fixtures(conn, config.capture_fixtures)
            result = {
                "batches": len(batches),
                "created": sum(b.created for b in batches),
                "statuses": sorted({b.status for b in batches}),
            }
        else:
            completed = replay_pending(conn)
            result = {"completed_pending": len(completed), "discrepancies": verify_replay(conn)}
            if result["discrepancies"]:
                print(json.dumps(result, sort_keys=True))
                return EXIT_DATABASE
    except (MigrationError, CaptureRejected, LookupError, OSError) as exc:
        print(f"eye: FAILED ({command}): {exc}", file=sys.stderr)
        return EXIT_DATABASE
    except (DatabaseError, InterfaceError) as exc:
        print(f"eye: FAILED ({command}): database error {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
