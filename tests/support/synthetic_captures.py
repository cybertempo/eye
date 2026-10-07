"""Build small invented capture documents for tests (synthetic-fixture source)."""

from __future__ import annotations

import json
from pathlib import Path


def record(record_id: str, observed: str, published: str, lon: float, lat: float = 0.0) -> dict:
    return {
        "record_id": record_id,
        "observed_time": observed,
        "source_published_time": published,
        "lon": lon,
        "lat": lat,
        "alt_m": 0,
        "confidence": 0.9,
        "quality_flags": [],
    }


def capture(
    directory: Path,
    name: str,
    layer: str,
    records: list[dict],
    *,
    start: str,
    end: str,
    received: str,
    bbox: tuple[float, float, float, float] = (-1.0, -1.0, 1.0, 1.0),
    status: str = "ok",
) -> Path:
    """Write one committed-looking capture: every record is invented."""
    doc = {
        "capture_format": "eye.synthetic-capture/2",
        "synthetic": True,
        "source_id": "synthetic-fixture",
        "adapter_version": "synthetic-adapter/2",
        "note": f"Test capture {name} ({layer}).",
        "layer": layer,
        "request": {
            "bbox": list(bbox),
            "start": start,
            "end": end,
            "expected_interval_s": 600,
        },
        "attempt": {
            "written_by": "EYE capture adapter; finished_at is the EYE receipt time",
            "started_at": received,
            "finished_at": received,
            "provider_status": status,
            "quota_cost": 0,
            "observed_start": start,
            "observed_end": end,
        },
        "provider_response": {"records": records},
    }
    path = directory / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path
