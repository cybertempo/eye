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
    assert all(e["source"] == "synthetic-events" for e in snapshot["events"])


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
    corrected = [e for e in snapshot["events"] if len(e["claims"]) > 1]
    assert corrected
    for event in corrected:
        claims = event["claims"]
        assert [c["version"] for c in claims] == sorted(c["version"] for c in claims)
        assert [c["published_time"] for c in claims] == sorted(c["published_time"] for c in claims)
        current = [c["claim_id"] for c in claims if c["is_current"]]
        assert current == [event["current_claim_id"]] == [claims[-1]["claim_id"]]
        first, last = claims[0], claims[-1]
        assert first["reported_event_location"] != last["reported_event_location"]


def test_demo_contains_brief_cases(snapshot):
    kinds = {c["kind"] for e in snapshot["events"] for c in e["claims"]}
    assert "road_closure" in kinds
    # A replayed delivery is a second receipt of the same claim, not a new claim.
    assert any(len(c["evidence_batch_ids"]) >= 2 for e in snapshot["events"] for c in e["claims"])
    assert {t["kind"] for t in snapshot["tracks"]} >= {"flight", "vessel"}


def test_motion_candidate_is_never_an_accident(snapshot):
    candidates = [e for e in snapshot["events"] if e["standing"] == "review_candidate"]
    assert candidates
    for event in candidates:
        for c in event["claims"]:
            assert c["basis"] == "motion_inference" and c["status"] == "candidate"
            assert c["kind"] not in {"aviation_accident", "marine_casualty", "road_collision"}
