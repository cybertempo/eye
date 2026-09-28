#!/usr/bin/env python3
"""Check the files Git would publish for material that must not be public.

Runs on: developer laptop, CI. Standard library only; no network.
It inspects staged Git blobs, working files and untracked files that are not
ignored, so it can run before a commit. It is a backstop for human review,
not a replacement.
Exit status: 0 clean, 1 findings, 2 the check itself could not run (UNVERIFIED).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024

# A project licence is the owner's decision (docs/public-repo-build-brief.md §2).
FORBIDDEN_NAME = re.compile(
    r"(^|/)(LICEN[CS]E|COPYING)(\.[A-Za-z0-9]+)?$"
    r"|(^|/)\.env(\.|$)(?!example$)"
    r"|\.(pem|key|p12|pfx|jks|keystore|kdbx|sqlite3?|db|pcap|pcapng)$"
    r"|(^|/)id_(rsa|ed25519|ecdsa)(\.pub)?$"
    r"|(^|/)\.claude/settings\.local\.json$"
    r"|(^|/)(credentials|secrets?)\.(json|toml|ya?ml)$",
    re.IGNORECASE,
)

CONTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Anthropic/OpenAI style key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_\-]{32,}")),
    (
        "private network address",
        re.compile(
            r"\b(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
            r"|\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"
        ),
    ),
    (
        "private hostname",
        re.compile(r"\b[\w-]+(\.[\w-]+)*\.(ts\.net|home\.arpa|internal|lan)\b", re.IGNORECASE),
    ),
    (
        "automatic browser launch",
        re.compile(
            r"\bimport\s+webbrowser\b|\bwebbrowser\.open|\bxdg-open\b|\bopen_browser\s*=\s*True"
        ),
    ),
]

# Only these files describe the project's status; they must not claim a licence.
STATUS_FILES = ("README.md", "NOTICE.md")
OPEN_SOURCE_CLAIM = re.compile(r"\bopen[- ]source\b", re.IGNORECASE)
NEGATION = re.compile(r"\b(not|no|never|isn't|is not|until)\b", re.IGNORECASE)


def publishable_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    names = [n for n in result.stdout.decode("utf-8").split("\0") if n]
    return sorted(set(names))


def staged_files(root: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    entries: dict[str, str] = {}
    for entry in result.stdout.decode("utf-8").split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, _object_id, stage = metadata.split()
        if stage != "0":
            raise RuntimeError(f"{name}: unresolved index conflict")
        entries[name] = mode
    return entries


def staged_content(root: Path, name: str) -> tuple[int, bytes | None]:
    """Read the index blob, not the possibly cleaner working-tree version."""
    reference = f":{name}"
    size_result = subprocess.run(
        ["git", "cat-file", "-s", reference],
        cwd=root,
        capture_output=True,
        check=True,
    )
    size = int(size_result.stdout)
    if size > MAX_FILE_BYTES:
        return size, None
    blob_result = subprocess.run(
        ["git", "cat-file", "blob", reference],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return size, blob_result.stdout


def check_content(name: str, version: str, content: bytes, findings: list[str]) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(f"{name} ({version}): binary file; add only reviewed assets via NOTICE.md")
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern in CONTENT_RULES:
            if pattern.search(line):
                findings.append(f"{name} ({version}):{lineno}: {label}")
        if name in STATUS_FILES and OPEN_SOURCE_CLAIM.search(line) and not NEGATION.search(line):
            findings.append(f"{name} ({version}):{lineno}: describes the project as open source")
    if name == "README.md" and "no licence" not in text.lower():
        findings.append(f"README.md ({version}): must state the current no-licence status")


def check(root: Path) -> list[str]:
    findings: list[str] = []
    files = publishable_files(root)
    if not files:
        raise RuntimeError("no files found; nothing was checked")
    staged = staged_files(root)
    checked_status: set[str] = set()

    for name in files:
        if FORBIDDEN_NAME.search(name):
            findings.append(f"{name}: file type or name must not be published")
        if name in staged:
            if staged[name] not in ("100644", "100755"):
                findings.append(f"{name} (staged): unsupported Git mode {staged[name]}")
            size, content = staged_content(root, name)
            if content is None:
                findings.append(f"{name} (staged): {size} bytes exceeds {MAX_FILE_BYTES}")
            else:
                check_content(name, "staged", content, findings)
                checked_status.add(name)
        path = root / name
        if path.is_symlink():
            findings.append(f"{name}: symlink must not be published")
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            findings.append(
                f"{name} (working tree): {size} bytes exceeds {MAX_FILE_BYTES}; no bulk data in git"
            )
            continue
        check_content(name, "working tree", path.read_bytes(), findings)
        checked_status.add(name)

    for name in STATUS_FILES:
        if name not in checked_status:
            findings.append(f"{name}: required status file is missing")
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    try:
        findings = check(root)
    except (OSError, RuntimeError, ValueError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f"boundary: UNVERIFIED, the check could not run: {exc}", file=sys.stderr)
        return 2
    if findings:
        print("boundary: FAILED")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print(f"boundary: OK ({len(publishable_files(root))} publishable files checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
