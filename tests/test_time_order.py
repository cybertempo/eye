"""O55: the browser orders wire timestamps chronologically, fractions included.

The compiled comparator (web/dist/facts.js) is checked against Python's
datetime, an independent parser. Plain string order is shown to disagree on the
fractional cases (so they test something) and to agree on the whole-second
controls.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime

from conftest import REPO_ROOT

HELPER = REPO_ROOT / "tests" / "support" / "time_check.mjs"
FRACTIONAL = [
    ("2026-01-01T03:04:00Z", "2026-01-01T03:04:00.500000Z"),
    ("2026-01-01T03:04:00.5Z", "2026-01-01T03:04:00Z"),
    ("2026-01-01T03:04:00Z", "2026-01-01T03:04:00.000001Z"),
    ("2026-01-01T03:04:00.5Z", "2026-01-01T03:04:00.500001Z"),
]
EQUAL = [("2026-01-01T03:04:00.5Z", "2026-01-01T03:04:00.500000Z")]
WHOLE = [
    ("2026-01-01T03:04:00Z", "2026-01-01T03:05:00Z"),
    ("2026-01-01T23:59:59Z", "2026-01-02T00:00:00Z"),
    ("2026-01-01T03:04:00Z", "2026-01-01T03:04:00Z"),
    ("2026-01-01T03:04:00.999999Z", "2026-01-01T03:04:01Z"),
]


def sign(value: float) -> int:
    return (value > 0) - (value < 0)


def chronological(a: str, b: str) -> int:
    return sign((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds())


def browser_order(pairs) -> list[int]:
    node = shutil.which("node")
    assert node, "Node.js is required (scripts/setup.sh)"
    result = subprocess.run(
        [node, str(HELPER), json.dumps(pairs)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


def test_browser_comparator_is_chronological_with_fractions():
    pairs = FRACTIONAL + EQUAL + WHOLE
    assert browser_order(pairs) == [chronological(a, b) for a, b in pairs]


def test_string_order_is_wrong_only_for_fractional_cases():
    for a, b in FRACTIONAL:
        assert sign((a > b) - (a < b)) != chronological(a, b), (a, b)
    for a, b in WHOLE[:3]:  # control: same width, string order is chronological
        assert sign((a > b) - (a < b)) == chronological(a, b), (a, b)
