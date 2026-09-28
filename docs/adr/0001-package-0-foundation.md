# ADR 0001: Package 0 foundation choices

Date: 2026-09-28. Status: proposed (awaiting owner review).

## Decisions

1. **Standard-library runtime for the demo.** The demo server, configuration
   loader and auth port use only the Python standard library. This keeps the
   first public build free of runtime third-party code. The API framework,
   database driver and schema tooling are chosen in Package 1 or 3 with their
   own licence review.
2. **One TOML configuration file** validated with unknown-key rejection. Unsafe
   combinations stop the process before it binds a socket.
3. **Auth port with no fallback.** `DemoAuth` refuses to construct outside demo
   mode. Production mode imports a privately installed module named in private
   configuration and refuses if it is missing, lacks `create_auth_port`, or
   returns demo auth. Production serving is not implemented yet and exits after
   the adapter check.
4. **Container binds all interfaces only inside its namespace.** Allowed only
   when both `server.container_internal_bind = true` and `EYE_CONTAINER=1`
   (set by the Dockerfile). Compose publishes on `127.0.0.1` only; a test
   enforces it and the smoke test checks the published address.
5. **Hash-locked tools and digest-pinned images/actions** (see
   `docs/dependencies.md`).
6. **Repository boundary check** (`scripts/check_repo_boundary.py`) scans the
   files Git would publish for licence files, secrets, private addresses and
   hostnames, automatic browser launch and oversize files. gitleaks scans full
   history in CI. Both back up human review; neither replaces it.

## Consequences

The demo is a table view of one synthetic snapshot, not THEATRE or DESK. The
fixture's field names are provisional until the Package 1 wire schema replaces
them with generated types and runtime validation.
