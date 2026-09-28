"""Invariants of the synthetic demo fixture (ahead of the Package 1 wire schema)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic" / "demo_snapshot.json"


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_marked_synthetic(snapshot):
    assert snapshot["synthetic"] is True
    assert all(t["source"] == "synthetic-fixture" for t in snapshot["tracks"])
    assert all(e["source"] == "synthetic-fixture" for e in snapshot["events"])


def test_source_and_receipt_times_are_separate(snapshot):
    for track in snapshot["tracks"]:
        for point in track["points"]:
            assert point["observed_time"] and point["received_time"]
            assert point["observed_time"] <= point["received_time"]


def test_outage_is_unknown_not_zero(snapshot):
    gaps = [c for c in snapshot["coverage"] if c["state"] == "unknown"]
    assert gaps, "fixture must contain a coverage gap"
    assert all(c["metric"]["value"] is None for c in gaps)
    qualified = [c for c in snapshot["coverage"] if c["state"] == "qualified"]
    assert qualified and all(isinstance(c["metric"]["value"], int) for c in qualified)


def test_late_correction_keeps_history(snapshot):
    corrected = [e for e in snapshot["events"] if e["status"] == "corrected"]
    assert corrected
    for event in corrected:
        revisions = event["revisions"]
        assert len(revisions) >= 2
        assert [r["revision"] for r in revisions] == sorted(r["revision"] for r in revisions)
        assert revisions[-1]["location"] == event["reported_event_location"]
        assert revisions[0]["location"] != revisions[-1]["location"]


def test_demo_contains_brief_cases(snapshot):
    kinds = {e["kind"] for e in snapshot["events"]}
    assert "road_closure" in kinds
    assert any(e["replayed"] for e in snapshot["events"])
    assert {t["kind"] for t in snapshot["tracks"]} >= {"flight", "vessel"}
