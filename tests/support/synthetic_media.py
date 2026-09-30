"""Build small invented news and media captures for tests (synthetic-news source).

Every publisher, headline, person and URL here is invented; URLs use the
reserved ``.invalid`` top-level domain, which can never resolve.
"""

from __future__ import annotations

import json
from pathlib import Path

DAY = "2026-02-01T"


def place(
    lon: float,
    lat: float,
    precision: float = 2000,
    role: str = "event_place",
    method: str = "source_stated",
) -> dict:
    return {"role": role, "method": method, "coords": [lon, lat], "precision_m": precision}


def item(
    item_id: str,
    published: str,
    *,
    kind: str = "article",
    status: str = "published",
    revised: str | None = None,
    publisher: str = "Synthetic Daily",
    headline: str | None = "auto",
    creator: str | None = None,
    url: str | None = None,
    syndicated_from: str | None = None,
    rights: dict | None = None,
    captured: str | None = None,
    where: dict | None = None,
    language: str | None = "en",
) -> dict:
    """One item version. ``revised`` defaults to the first publication."""
    return {
        "item_id": item_id,
        "kind": kind,
        "status": status,
        "first_published_time": published,
        "revision_time": revised or published,
        "url": url or f"https://news.invalid/{item_id.lower()}",
        "syndicated_from": syndicated_from,
        "publisher": publisher,
        "creator": creator,
        "headline": f"Invented report {item_id}" if headline == "auto" else headline,
        "language": language,
        "rights": rights or {"status": "link_only"},
        "capture_time_claimed": captured,
        "place": where,
    }


def licensed(licence: str = "CC-BY-4.0", attribution: str = "Invented Photographer") -> dict:
    return {"status": "licensed", "licence": licence, "attribution": attribution}


def capture(
    directory: Path,
    name: str,
    items: list[dict],
    *,
    source: str = "synthetic-news",
    start: str = DAY + "12:00:00Z",
    end: str = DAY + "13:00:00Z",
    received: str | None = None,
    status: str = "ok",
    bbox=(-0.5, -0.5, 0.5, 0.5),
) -> Path:
    """Write one news capture: every item is invented."""
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
        "capture_format": "eye.synthetic-media-items/1",
        "synthetic": True,
        "source_id": source,
        "adapter_version": "synthetic-news/1",
        "note": f"Test news capture {name}.",
        "layer": "news",
        "request": {"bbox": list(bbox), "start": start, "end": end, "expected_interval_s": 3600},
        "attempt": attempt,
        "provider_response": {"items": items},
    }
    path = directory / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path
