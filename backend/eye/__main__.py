"""Command-line entry point: ``python -m eye {check-config,serve} --config PATH``.

Exit codes: 0 success, 2 configuration refused, 3 authentication adapter
refused, 4 other startup refusal. Nothing here opens a browser.
"""

from __future__ import annotations

import argparse
import sys

from eye.api.auth import AuthUnavailable, resolve_auth
from eye.config import ConfigError, EyeConfig, load_config

EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_STARTUP = 4


def _prepare(path: str) -> tuple[EyeConfig, object]:
    config = load_config(path)
    return config, resolve_auth(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eye")
    parser.add_argument("command", choices=("check-config", "serve"))
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


if __name__ == "__main__":
    sys.exit(main())
