#!/usr/bin/env python3
"""Check that web/dist is exactly what web/src compiles to. Runs on: developer laptop, CI.

The headless browser tests load the compiled JavaScript in web/dist, not the
TypeScript in web/src. A bundle built before the source last changed would
test old code, so scripts/verify.sh rebuilds web/dist and then runs this check.

The check compiles web/src afresh into a temporary directory with the project's
own TypeScript compiler and compares it with web/dist file by file. A file that
differs, is missing or is left over from deleted source fails the check.
Makes no network calls.

Usage: check_web_dist.py [--web DIR]   (default: the repository's web/)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def files(directory: Path) -> dict[str, bytes]:
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def stale(web: Path) -> list[str]:
    """Problems with ``web/dist``; empty when it matches a fresh build of ``web/src``."""
    tsc = web / "node_modules" / ".bin" / "tsc"
    if not tsc.exists():
        return [f"{tsc} not found; run scripts/setup.sh first"]
    with tempfile.TemporaryDirectory(prefix="eye-web-build-") as out:
        result = subprocess.run(
            [str(tsc), "-p", str(web / "tsconfig.json"), "--outDir", out],
            cwd=web,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ["web/src does not compile:\n" + (result.stdout + result.stderr).strip()]
        fresh = files(Path(out))
    built = files(web / "dist")
    problems = []
    for name in sorted(fresh.keys() | built.keys()):
        if name not in built:
            problems.append(f"dist/{name} is missing")
        elif name not in fresh:
            problems.append(f"dist/{name} has no source (left over from deleted code)")
        elif built[name] != fresh[name]:
            problems.append(f"dist/{name} differs from a fresh build of web/src")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--web", type=Path, default=ROOT / "web")
    args = parser.parse_args()
    problems = stale(args.web.resolve())
    if problems:
        print("web/dist is stale; run `npm run --prefix web build`:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("web/dist matches a fresh build of web/src")
    return 0


if __name__ == "__main__":
    sys.exit(main())
