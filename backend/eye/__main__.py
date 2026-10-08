"""Command-line entry point: ``python -m eye COMMAND --config PATH``.

Commands: check-config, serve, db-migrate, db-status, db-load-fixtures,
db-derive-transits, db-prepare-demo (migrate, load fixtures and derive, demo only),
(db-migrate and db-prepare-demo claim the database for the demo or a private
installation; demo mode refuses a private or real database before writing),
db-replay, and the Package 5 lifecycle commands: db-rollup, db-manifest-check,
db-backup, db-restore-drill (synthetic code test), db-retention-plan (read-only)
and db-retention-execute (refused unless retention.allow_deletion is true, in
demo mode, for one explicitly named synthetic partition); and the research
commands db-backtest (records a backtest of coverage-weighted baselines over
current, valid rollup manifests) and db-backtest-replay (recomputes a recorded
run from the manifests it cites). Exit codes: 0 success,
2 configuration refused, 3 authentication adapter refused, 4 other startup
refusal, 5 database operation failed.
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
DB_COMMANDS = (
    "db-migrate",
    "db-status",
    "db-load-fixtures",
    "db-derive-transits",
    "db-prepare-demo",
    "db-replay",
    "db-rollup",
    "db-manifest-check",
    "db-backup",
    "db-restore-drill",
    "db-retention-plan",
    "db-retention-execute",
    "db-backtest",
    "db-backtest-replay",
)
LIFECYCLE_COMMANDS = DB_COMMANDS[6:12]
RESEARCH_COMMANDS = DB_COMMANDS[12:]


def _prepare(path: str) -> tuple[EyeConfig, object]:
    config = load_config(path)
    return config, resolve_auth(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eye")
    parser.add_argument("command", choices=("check-config", "serve", *DB_COMMANDS))
    parser.add_argument("--config", required=True, help="path to an EYE TOML configuration")
    parser.add_argument("--partition", help="db-retention-execute: SOURCE:LAYER:YYYY-MM-DD")
    parser.add_argument("--generation", help="db-restore-drill: backup generation id")
    parser.add_argument("--series", help="db-backtest: SOURCE:LAYER:DERIVATION[:SCOPE]")
    parser.add_argument("--start", help="db-backtest: first target day, YYYY-MM-DD")
    parser.add_argument("--end", help="db-backtest: last target day, YYYY-MM-DD")
    parser.add_argument("--run", help="db-backtest-replay: backtest run id")
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

    if args.command in LIFECYCLE_COMMANDS:
        return _lifecycle_command(args, config)
    if args.command in RESEARCH_COMMANDS:
        return _research_command(args, config)
    if args.command in DB_COMMANDS:
        return _database_command(args.command, config)

    # Both modes need the database; production has already passed the private
    # authentication gate above (resolve_auth never falls back to demo auth).
    import os

    database_url = os.environ.get(config.database_url_env)
    if not database_url:
        print(
            f"eye: REFUSED (configuration): environment variable {config.database_url_env} "
            "is not set; the API serves database-backed data only",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    from eye.api.server import StartupError, build_server

    try:
        server = build_server(config, auth, database_url)
    except (StartupError, OSError) as exc:
        print(f"eye: REFUSED (startup): {exc}", file=sys.stderr)
        return EXIT_STARTUP

    host, port = server.server_address[:2]
    shown = f"[{host}]" if ":" in host else host
    label = "synthetic demo" if config.mode == "demo" else "production API (loopback)"
    print(f"eye: {label} listening on http://{shown}:{port}/ (open it yourself)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _demo_target_refusal(conn, config: EyeConfig) -> int | None:
    """In demo mode, refuse a private or real database before reading further or writing."""
    from eye.storage import identity

    if config.mode != "demo":
        return None
    try:
        identity.check_demo_target(conn)
    except identity.IdentityRefused as exc:
        conn.close()
        print(f"eye: REFUSED (database): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # a failed check is never a pass
        conn.close()
        print(f"eye: UNVERIFIED (database identity): {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    return None


def _database_command(command: str, config: EyeConfig) -> int:
    import json
    import os

    from pg8000.exceptions import DatabaseError, InterfaceError

    from eye.ingest.capture import CaptureRejected, load_fixtures, replay_pending, verify_replay
    from eye.storage import identity
    from eye.storage.db import DatabaseConfigError, connect
    from eye.storage.migrate import MigrationError, migrate, status
    from eye.worker import transits

    kind = identity.DEMO if config.mode == "demo" else identity.PRIVATE

    url = os.environ.get(config.database_url_env)
    if not url:
        print(
            f"eye: REFUSED (configuration): environment variable {config.database_url_env} "
            "is not set",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    if command in ("db-load-fixtures", "db-prepare-demo"):
        if config.mode != "demo":
            print(
                "eye: REFUSED (configuration): synthetic fixtures load in demo mode only",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        if config.capture_fixtures is None:
            print("eye: REFUSED (configuration): data.capture_fixtures is not set", file=sys.stderr)
            return EXIT_CONFIG
    if command in ("db-derive-transits", "db-prepare-demo") and config.count_lines is None:
        print("eye: REFUSED (configuration): data.count_lines is not set", file=sys.stderr)
        return EXIT_CONFIG
    try:
        conn = connect(url, require_loopback=config.mode == "demo")
    except DatabaseConfigError as exc:
        print(f"eye: REFUSED (configuration): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # connection failures are reported, never treated as empty
        print(f"eye: UNVERIFIED (database unreachable): {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    refused = _demo_target_refusal(conn, config)
    if refused is not None:
        return refused
    try:
        if command == "db-migrate":
            result = {"applied_now": migrate(conn), "database": identity.claim(conn, kind)}
        elif command == "db-prepare-demo":
            applied = migrate(conn)
            identity.claim(conn, identity.DEMO)
            batches = load_fixtures(conn, config.capture_fixtures)
            if config.ais_capture_fixtures is not None:
                batches += load_fixtures(conn, config.ais_capture_fixtures)
            if config.event_capture_fixtures is not None:
                batches += load_fixtures(conn, config.event_capture_fixtures)
            if config.media_capture_fixtures is not None:
                batches += load_fixtures(conn, config.media_capture_fixtures)
            runs = []
            for line in _lines(transits, config.count_lines):
                intervals = transits.hourly_intervals(
                    transits.read_inputs(conn, transits.SOURCE_ID, line)
                )
                runs.append(transits.store(conn, transits.SOURCE_ID, line, intervals)[0])
            result = {"applied_now": applied, "batches": len(batches), "runs": runs}
        elif command == "db-status":
            result = status(conn)
        elif command == "db-load-fixtures":
            batches = load_fixtures(conn, config.capture_fixtures)
            if config.ais_capture_fixtures is not None:
                batches += load_fixtures(conn, config.ais_capture_fixtures)
            if config.event_capture_fixtures is not None:
                batches += load_fixtures(conn, config.event_capture_fixtures)
            if config.media_capture_fixtures is not None:
                batches += load_fixtures(conn, config.media_capture_fixtures)
            result = {
                "batches": len(batches),
                "created": sum(b.created for b in batches),
                "statuses": sorted({b.status for b in batches}),
            }
        elif command == "db-derive-transits":
            result = {"lines": []}
            for line in _lines(transits, config.count_lines):
                intervals = transits.hourly_intervals(
                    transits.read_inputs(conn, transits.SOURCE_ID, line)
                )
                run_id, derived = transits.store(conn, transits.SOURCE_ID, line, intervals)
                result["lines"].append(
                    {
                        "line": f"{line.line_id}/v{line.version}",
                        "run_id": run_id,
                        "crossings": len(derived.crossings),
                        "counts": [
                            {"start": c[0], "end": c[1], "state": c[2], "total": c[5]}
                            for c in sorted(derived.counts.values())
                        ],
                    }
                )
        else:
            completed = replay_pending(conn)
            discrepancies = verify_replay(conn)
            audited = 0
            for line in _lines(transits, config.count_lines) if config.count_lines else []:
                discrepancies += transits.verify(conn, transits.SOURCE_ID, line)
                for run_id in transits.recorded_runs(conn, transits.SOURCE_ID, line):
                    discrepancies += transits.audit_run(conn, run_id, line)
                    audited += 1
            result = {
                "completed_pending": len(completed),
                "discrepancies": discrepancies,
                "audited_runs": audited,
            }
            if result["discrepancies"]:
                print(json.dumps(result, sort_keys=True))
                return EXIT_DATABASE
    except identity.IdentityRefused as exc:
        print(f"eye: REFUSED (database): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (
        MigrationError,
        CaptureRejected,
        LookupError,
        OSError,
        transits.LineDefinitionError,
    ) as exc:
        print(f"eye: FAILED ({command}): {exc}", file=sys.stderr)
        return EXIT_DATABASE
    except (DatabaseError, InterfaceError) as exc:
        print(f"eye: FAILED ({command}): database error {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True))
    return 0


def _lifecycle_command(args, config: EyeConfig) -> int:
    """Package 5: rollups, manifests, synthetic backup and drill, checked retention."""
    import json
    import os
    from pathlib import Path

    from pg8000.exceptions import DatabaseError, InterfaceError

    from eye.storage.db import DatabaseConfigError, connect
    from eye.worker import backup, retention, rollups, transits

    command = args.command
    ret = config.retention
    url = os.environ.get(config.database_url_env)
    if not url:
        print(
            f"eye: REFUSED (configuration): environment variable {config.database_url_env} "
            "is not set",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    backup_value = os.environ.get(ret.backup_dir_env)
    backup_dir = Path(backup_value) if backup_value else None
    if command in ("db-backup", "db-restore-drill") and backup_dir is None:
        print(
            f"eye: REFUSED (configuration): environment variable {ret.backup_dir_env} "
            "(the synthetic backup directory) is not set",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    if command in ("db-backup", "db-restore-drill") and config.mode != "demo":
        print(
            "eye: REFUSED (configuration): the synthetic backup and drill run in demo mode only",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    if command == "db-retention-execute":
        if not args.partition:
            print(
                "eye: REFUSED (configuration): --partition SOURCE:LAYER:YYYY-MM-DD is "
                "required; retention never selects partitions itself",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        try:
            partition = rollups.Partition.parse(args.partition)
        except ValueError as exc:
            print(f"eye: REFUSED (configuration): {exc}", file=sys.stderr)
            return EXIT_CONFIG
    if command == "db-restore-drill" and not args.generation:
        print("eye: REFUSED (configuration): --generation ID is required", file=sys.stderr)
        return EXIT_CONFIG
    lines = _lines(transits, config.count_lines) if config.count_lines else []
    try:
        conn = connect(url, require_loopback=config.mode == "demo")
    except DatabaseConfigError as exc:
        print(f"eye: REFUSED (configuration): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # connection failures are reported, never treated as empty
        print(f"eye: UNVERIFIED (database unreachable): {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    refused = _demo_target_refusal(conn, config)
    if refused is not None:
        return refused
    failed = False
    try:
        if command == "db-rollup":
            results = rollups.refresh_all(conn, lines, ret.lateness_hours)
            failed = any(r.error for r in results)
            result = {
                "manifests": [
                    {
                        "partition": r.partition.key,
                        "derivation": r.derivation,
                        "scope": r.scope,
                        "manifest_id": r.manifest_id,
                        "created": r.created,
                        "error": r.error,
                    }
                    for r in results
                ]
            }
        elif command == "db-manifest-check":
            checks = []
            for p in rollups.partitions(conn):
                for day in rollups.days_touched(conn, p):
                    checks += rollups.validate(
                        conn, rollups.Partition(p.source_id, p.layer, day), lines
                    )
            from eye.storage.db import transaction

            with transaction(conn):
                rollups.record_checks(conn, checks)
            checks = sorted(set(checks), key=lambda c: c.label)
            failed = any(c.state != "valid" for c in checks)
            result = {
                "checks": [{"check": c.label, "state": c.state, "reason": c.reason} for c in checks]
            }
        elif command == "db-backup":
            generation = backup.export(conn, backup_dir, config.count_lines)
            proof = backup.verify(conn, backup_dir, generation)
            failed = proof.state != "verified"
            result = {
                "label": backup.LABEL,
                "generation_id": generation,
                "proof_id": proof.proof_id,
                "state": proof.state,
                "reason": proof.reason,
            }
        elif command == "db-restore-drill":
            target_url = os.environ.get(ret.restore_url_env)
            if not target_url:
                print(
                    f"eye: REFUSED (configuration): environment variable "
                    f"{ret.restore_url_env} (an empty restore database) is not set",
                    file=sys.stderr,
                )
                return EXIT_CONFIG
            if target_url == url:
                print(
                    "eye: REFUSED (configuration): the restore database must not be the "
                    "main database",
                    file=sys.stderr,
                )
                return EXIT_CONFIG
            target = connect(target_url, require_loopback=True)
            try:
                report = backup.restore_drill(
                    backup_dir, args.generation, target, ret.lateness_hours
                )
            finally:
                target.close()
            failed = report.state != "verified"
            result = report.as_dict()
        elif command == "db-retention-plan":
            verdicts = retention.plan(
                conn, lateness_hours=ret.lateness_hours, lines=lines, backup_dir=backup_dir
            )
            result = {
                "read_only": True,
                "deletion_enabled": ret.allow_deletion,
                "partitions": [v.as_dict() for v in verdicts],
            }
        else:
            outcome = retention.execute(
                conn,
                partition,
                allow_deletion=ret.allow_deletion,
                mode=config.mode,
                lateness_hours=ret.lateness_hours,
                lines=lines,
                backup_dir=backup_dir,
            )
            failed = outcome.verdict.verdict != "eligible"
            result = outcome.as_dict()
    except retention.RetentionRefused as exc:
        print(f"eye: REFUSED (retention): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (backup.BackupError, OSError, LookupError, transits.LineDefinitionError) as exc:
        print(f"eye: FAILED ({command}): {exc}", file=sys.stderr)
        return EXIT_DATABASE
    except (DatabaseError, InterfaceError) as exc:
        print(f"eye: FAILED ({command}): database error {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True, default=str))
    return EXIT_DATABASE if failed else 0


def _research_command(args, config: EyeConfig) -> int:
    """Package 5 research: record a backtest, or replay a recorded one."""
    import json
    import os
    from datetime import date

    from pg8000.exceptions import DatabaseError, InterfaceError

    from eye.storage.db import DatabaseConfigError, connect
    from eye.worker import backtest, baselines, transits

    res = config.research
    url = os.environ.get(config.database_url_env)
    if not url:
        print(
            f"eye: REFUSED (configuration): environment variable {config.database_url_env} "
            "is not set",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    try:
        if args.command == "db-backtest":
            if not (args.series and args.start and args.end):
                raise baselines.BaselineRefused("--series, --start and --end are required")
            series = backtest.Series.parse(args.series)
            first, last = date.fromisoformat(args.start), date.fromisoformat(args.end)
            params = baselines.Params(
                bin_hours=res.bin_hours,
                history_days=res.history_days,
                min_history_bins=res.min_history_bins,
                threshold_sigma=res.threshold_sigma,
                min_excess=res.min_excess,
                min_target_coverage_pct=res.min_target_coverage_pct,
            )
            limits = backtest.Limits(res.max_days, res.max_output_rows)
            backtest.check_bounds(first, last, params, limits)
        elif not args.run:
            raise baselines.BaselineRefused("--run ID is required")
    except (baselines.BaselineRefused, ValueError) as exc:
        print(f"eye: REFUSED (configuration): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    lines = _lines(transits, config.count_lines) if config.count_lines else []
    try:
        conn = connect(url, require_loopback=config.mode == "demo")
    except DatabaseConfigError as exc:
        print(f"eye: REFUSED (configuration): {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # connection failures are reported, never treated as empty
        print(f"eye: UNVERIFIED (database unreachable): {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    refused = _demo_target_refusal(conn, config)
    if refused is not None:
        return refused
    try:
        if args.command == "db-backtest":
            run = backtest.run_backtest(conn, series, first, last, params, limits, lines)
            result, failed = run.as_dict(), False
        else:
            report = backtest.replay(conn, args.run)
            result, failed = report.as_dict(), not report.identical
    except backtest.BacktestRefused as exc:
        print(f"eye: REFUSED (backtest): {exc}", file=sys.stderr)
        return EXIT_DATABASE
    except (DatabaseError, InterfaceError) as exc:
        print(f"eye: FAILED ({args.command}): database error {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True))
    return EXIT_DATABASE if failed else 0


def _lines(transits, directory):
    return [transits.load_line(path) for path in sorted(directory.glob("*.json"))]


if __name__ == "__main__":
    sys.exit(main())
