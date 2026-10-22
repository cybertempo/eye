#!/usr/bin/env python3
"""Derive the facts the architecture map shows from the repository's own files.

Runs on: developer laptop, CI (through the tests), and the scheduled Claude cloud
session that refreshes the published map. Standard library only; no network.
  scripts/gen_system_map.py                  print the facts as JSON
  scripts/gen_system_map.py --out FILE       write them to FILE
  scripts/gen_system_map.py --root DIR       read another checkout

Only deterministic facts: file paths, line counts, first docstring or comment
lines, internal imports, tables and views each migration creates, the command
line's commands and the API routes. Descriptions, layout and tours stay
hand-written in docs/architecture/system-map.html, which merges these facts
over its own snapshot. A file that cannot be parsed is listed with
"parse_error": true (UNVERIFIED), never dropped.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

FORMAT = "eye.system-map-facts/1"
REPO = "cybertempo/eye"
MAX_DOC = 200

# Areas the map covers, as (glob, kind). Tests are counted, not mapped.
AREAS = (
    ("backend/eye/**/*.py", "py"),
    ("web/src/**/*.ts", "ts"),
    ("migrations/*.sql", "sql"),
    ("schemas/*.json", "json"),
    ("scripts/*", "script"),
    (".github/workflows/*.yml", "yml"),
    ("deploy/**/*", "deploy"),
    ("config/*.toml", "toml"),
    ("reference/lines/*.json", "json"),
)
CREATE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:eye\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
TS_IMPORT = re.compile(r"""^\s*(?:import|export)\b[^;]*?from\s+["'](\.[^"']+)["']""", re.MULTILINE)
ROUTE = re.compile(r'f"\{API_PREFIX\}(/[a-z0-9_/-]+)"')
API_PREFIX = re.compile(r'^API_PREFIX\s*=\s*"([^"]+)"', re.MULTILINE)


def line_count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:MAX_DOC]
    return ""


def python_module_path(root: Path, module: str) -> str | None:
    """Map eye.a.b to its file, preferring a module over a package."""
    parts = module.split(".")
    if parts[0] != "eye":
        return None
    base = root / "backend" / Path(*parts)
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate.relative_to(root).as_posix()
    return None


def python_facts(root: Path, text: str) -> dict:
    tree = ast.parse(text)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            # from eye.api import feed names eye/api/feed.py, not the package itself
            subs = [python_module_path(root, f"{node.module}.{a.name}") for a in node.names]
            subs = [s for s in subs if s]
            found = None if subs else python_module_path(root, node.module)
            imports.update(subs + ([found] if found else []))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found = python_module_path(root, alias.name)
                if found:
                    imports.add(found)
    return {"doc": first_line(ast.get_docstring(tree)), "imports": sorted(imports)}


def ts_facts(path: Path, root: Path, text: str) -> dict:
    imports = set()
    for spec in TS_IMPORT.findall(text):
        target = (path.parent / spec).resolve().with_suffix(".ts")
        if target.is_file():
            imports.add(target.relative_to(root.resolve()).as_posix())
    doc = ""
    for line in text.splitlines():
        if line.startswith("//"):
            doc = line.lstrip("/ ").strip()[:MAX_DOC]
            break
        if line.strip():
            break
    return {"doc": doc, "imports": sorted(imports)}


def sql_facts(text: str) -> dict:
    tables: list[str] = []
    views: list[str] = []
    for kind, name in CREATE.findall(re.sub(r"--[^\n]*", "", text)):
        target = tables if kind.upper() == "TABLE" else views
        if name not in target:
            target.append(name)
    doc = first_line(text.split("\n", 1)[0].lstrip("- "))
    return {"doc": doc, "tables": tables, "views": views}


def comment_doc(text: str) -> str:
    for line in text.splitlines()[:6]:
        if line.startswith("#!"):
            continue
        if line.startswith("#"):
            return line.lstrip("# ").strip()[:MAX_DOC]
    return ""


def cli_commands(root: Path) -> list[str]:
    """The command line's choices, in order, from backend/eye/__main__.py."""
    path = root / "backend" / "eye" / "__main__.py"
    if not path.is_file():
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tuples: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
            values = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
            for target in node.targets:
                if isinstance(target, ast.Name):
                    tuples[target.id] = [v for v in values if isinstance(v, str)]
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant)):
            continue
        if node.args[0].value != "command":
            continue
        for keyword in node.keywords:
            if keyword.arg == "choices" and isinstance(keyword.value, ast.Tuple):
                out: list[str] = []
                for elt in keyword.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        out.append(elt.value)
                    elif isinstance(elt, ast.Starred) and isinstance(elt.value, ast.Name):
                        out.extend(tuples.get(elt.value.id, []))
                return out
    return []


def api_routes(root: Path) -> list[str]:
    path = root / "backend" / "eye" / "api" / "server.py"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    prefix = API_PREFIX.search(text)
    base = prefix.group(1) if prefix else ""
    routes: list[str] = []
    for suffix in ROUTE.findall(text):
        if base + suffix not in routes:
            routes.append(base + suffix)
    return routes


def git_commit(root: Path) -> tuple[str | None, str | None]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%H %cI"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None, None
    return (out[0], out[1]) if len(out) == 2 else (None, None)


def collect(root: Path) -> dict:
    files: list[dict] = []
    seen: set[str] = set()
    for pattern, kind in AREAS:
        for path in sorted(root.glob(pattern)):
            rel = path.relative_to(root).as_posix()
            if not path.is_file() or rel in seen or "/node_modules/" in rel or "__pycache__" in rel:
                continue
            seen.add(rel)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                files.append({"path": rel, "kind": kind, "parse_error": True})
                continue
            entry: dict = {"path": rel, "kind": kind, "loc": line_count(text)}
            try:
                if kind == "py" or rel.endswith(".py"):
                    entry.update(python_facts(root, text))
                elif kind == "ts":
                    entry.update(ts_facts(path, root, text))
                elif kind == "sql":
                    entry.update(sql_facts(text))
                elif kind in ("script", "deploy", "yml", "toml"):
                    entry["doc"] = comment_doc(text)
            except SyntaxError:
                entry["parse_error"] = True
            files.append(entry)
    commit, date = git_commit(root)
    schemas = sorted(
        int(m.group(1))
        for p in root.glob("schemas/eye-wire.v*.schema.json")
        if (m := re.search(r"\.v(\d+)\.", p.name))
    )
    return {
        "format": FORMAT,
        "repo": REPO,
        "commit": commit,
        "commit_date": date,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
        "cli_commands": cli_commands(root),
        "routes": api_routes(root),
        "wire_versions": schemas,
        "test_modules": len(list(root.glob("tests/test_*.py"))),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gen_system_map")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--out", help="write the facts here instead of standard output")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / "backend" / "eye").is_dir():
        print(f"gen_system_map: {root} is not an EYE checkout", file=sys.stderr)
        return 2
    facts = collect(root)
    text = json.dumps(facts, indent=1, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    unverified = sum(1 for f in facts["files"] if f.get("parse_error"))
    print(
        f"gen_system_map: {len(facts['files'])} files at {facts['commit'] or 'no commit'}"
        + (f"; {unverified} UNVERIFIED (could not parse)" if unverified else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
