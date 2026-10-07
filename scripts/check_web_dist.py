#!/usr/bin/env python3
"""Check that web/dist is a current build of web/src.

Runs on: developer laptop, CI. Needs scripts/setup.sh first (locked TypeScript
in web/node_modules); no network.
  scripts/check_web_dist.py               compare web/dist with a fresh build
  scripts/check_web_dist.py --dist DIR    compare DIR instead (used by tests)

It compiles web/src with the locked compiler into a temporary directory and
compares every file byte for byte with the build under test, so a stale,
missing, extra or hand-edited file fails whatever its timestamp says.
Exit status: 0 current, 1 stale, 2 the fresh build itself failed (UNVERIFIED).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DIST = WEB / "dist"
TSC = WEB / "node_modules" / "typescript" / "bin" / "tsc"
MAX_FILES = 500
BUILD_TIMEOUT_S = 120


class Unverified(Exception):
    """The fresh reference build could not be produced."""


def fresh_build(out_dir: Path) -> None:
    """Compile web/src into out_dir with the project's tsconfig."""
    if not TSC.is_file():
        raise Unverified(f"{TSC.relative_to(ROOT)} not found; run scripts/setup.sh")
    try:
        result = subprocess.run(
            ["node", str(TSC), "-p", str(WEB / "tsconfig.json"), "--outDir", str(out_dir)],
            cwd=WEB,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unverified(f"fresh build did not run: {exc}") from exc
    if result.returncode != 0:
        raise Unverified(f"fresh build failed:\n{result.stdout}{result.stderr}")


def list_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() or path.is_symlink():
            files[path.relative_to(root).as_posix()] = path
            if len(files) > MAX_FILES:
                raise Unverified(f"{root} holds more than {MAX_FILES} files")
    return files


def compare(expected_dir: Path, actual_dir: Path) -> list[str]:
    """Differences between a fresh build and the build under test; empty if current."""
    if not actual_dir.is_dir():
        return [f"{actual_dir} is missing"]
    expected = list_files(expected_dir)
    if not expected:
        raise Unverified("fresh build produced no files")
    actual = list_files(actual_dir)
    problems = [f"missing: {name}" for name in sorted(expected.keys() - actual.keys())]
    problems += [
        f"not built from web/src: {name}" for name in sorted(actual.keys() - expected.keys())
    ]
    for name in sorted(expected.keys() & actual.keys()):
        if actual[name].is_symlink() or not actual[name].is_file():
            problems.append(f"not a regular file: {name}")
        elif actual[name].read_bytes() != expected[name].read_bytes():
            problems.append(f"differs from web/src: {name}")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=DIST, help="build to check (default web/dist)")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="eye-web-dist-") as scratch:
            fresh = Path(scratch) / "dist"
            fresh_build(fresh)
            problems = compare(fresh, args.dist)
    except Unverified as exc:
        print(f"web dist: UNVERIFIED: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(f"web dist: {args.dist} is not a current build of web/src:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("  rebuild with: npm run --prefix web build", file=sys.stderr)
        return 1
    print(f"web dist: {args.dist} matches a fresh build of web/src")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
