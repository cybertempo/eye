"""Build small invented event-claim captures for tests (synthetic-events source)."""

from __future__ import annotations

import json
from pathlib import Path

DAY = "2026-02-01T"


def point(lon: float, lat: float, precision: float = 500) -> dict:
    return {"type": "point", "coords": [lon, lat], "precision_m": precision}


def area(lon: float, lat: float, half: float = 0.01, precision: float = 1000) -> dict:
    ring = [
        [lon - half, lat - half],
        [lon + half, lat - half],
        [lon + half, lat + half],
        [lon - half, lat + half],
        [lon - half, lat - half],
    ]
    return {"type": "area", "coords": ring, "precision_m": precision}


def segment(coords, direction: str = "forward", precision: float = 50) -> dict:
    return {
        "type": "segment",
        "coords": [list(c) for c in coords],
        "precision_m": precision,
        "direction": direction,
    }


def claim(
    case: str,
    kind: str,
    published: str,
    location: dict,
    *,
    basis: str = "official_report",
    status: str = "reported",
    evidence: str | None = "auto",
    event_time: str | None = None,
    uncertainty: int | None = 60,
    identifiers: list[str] | None = None,
    window: tuple[str, str] | None = None,
    summary: str | None = None,
) -> dict:
    if evidence == "auto":
        evidence = None if basis == "motion_inference" else f"synthetic-doc:{case}:{published}"
    subject = None
    if identifiers is not None or window is not None:
        subject = {"identifiers": identifiers or []}
        if window is not None:
            subject["window"] = {"start": window[0], "end": window[1]}
    return {
        "case_id": case,
        "basis": basis,
        "kind": kind,
        "status": status,
        "event_time": event_time,
        "event_time_uncertainty_s": None if event_time is None else uncertainty,
        "source_published_time": published,
        "location": location,
        "evidence_ref": evidence,
        "subject": subject,
        "summary": summary,
    }


def capture(
    directory: Path,
    name: str,
    layer: str,
    claims: list[dict],
    *,
    start: str = DAY + "12:00:00Z",
    end: str = DAY + "13:00:00Z",
    received: str | None = None,
    status: str = "ok",
    bbox=(-0.5, -0.5, 0.5, 0.5),
) -> Path:
    """Write one event-claim capture: every claim is invented."""
    received = received or end.replace(":00:00Z", ":00:10Z")
    attempt = {
        "written_by": "EYE capture adapter; finished_at is the EYE receipt time",
        "started_at": received,
        "finished_at": received,
        "provider_status": status,
        "quota_cost": 0,
    }
    if status == "ok":
        attempt.update({"observed_start": start, "observed_end": end})
    doc = {
        "capture_format": "eye.synthetic-event-claims/1",
        "synthetic": True,
        "source_id": "synthetic-events",
        "adapter_version": "synthetic-events/1",
        "note": f"Test event capture {name} ({layer}).",
        "layer": layer,
        "request": {"bbox": list(bbox), "start": start, "end": end, "expected_interval_s": 3600},
        "attempt": attempt,
        "provider_response": {"claims": claims},
    }
    path = directory / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path
