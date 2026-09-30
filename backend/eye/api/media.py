"""News and media items for the browser API (Package 4d).

Reads ``eye.media_item`` and its version view and returns ``MediaItem`` dicts,
kept apart from event cases. Rules, all conservative:

* Every version of an item is returned (updates, corrections, retractions,
  conflicting versions), each with its own source times, EYE receipt time,
  link to the original and the capture batches that delivered it.
* An item is in view when it was first published in the requested interval
  and either its latest place lies in the area, or it states no place and a
  capture that delivered it asked about the area.
* An item whose reuse rights are unknown is served as a link only
  (``headline_withheld``): its headline is never stored, so never shown.
* Matching across publishers is only ever a **suggestion**: two items may
  describe the same story, or one is a syndicated copy of the other. A
  suggestion never merges items, never counts as independent confirmation
  and never creates or confirms an event.
"""

from __future__ import annotations

import hashlib
import json
import math

MEDIA_METRIC = "media_items"  # stored per batch: distinct items in the whole capture
# Served instead: items in this view whose latest version came from that batch.
MEDIA_VIEW_METRIC = "media_items_in_view"
NEWS_LAYER = "news"
NO_NEWS_SOURCE = "no news source covers this area and time; items unknown, not absent"
MAX_ITEM_VERSIONS = 100  # wire schema MediaItem.versions maxItems
MAX_EVIDENCE = 100  # wire schema MediaVersion.evidence_batch_ids maxItems
MAX_SUGGESTIONS = 500  # wire schema SnapshotMessage.media_suggestions maxItems
SUGGESTION_WINDOW_S = 6 * 3600


class MediaRefused(ValueError):
    """Raised with a status and reason; feed.py turns it into QueryRefused."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


def media_id(source: str, item: str) -> str:
    digest = hashlib.sha256(json.dumps([source, item]).encode("utf-8")).hexdigest()
    return f"med-{digest[:32]}"


# The view envelope, written out in each query.
ITEMS_IN_VIEW = """
SELECT DISTINCT m.source_id, m.item_id
FROM eye.media_item m JOIN eye.media_item_version v USING (media_item_id)
WHERE v.is_current IS NOT FALSE
  AND m.first_published_time >= :start AND m.first_published_time < :end
  AND (CASE WHEN m.place IS NOT NULL
            THEN ST_Intersects(m.place, ST_MakeEnvelope(:w, :s, :e, :n, 4326))
            ELSE EXISTS (
              SELECT 1 FROM eye.media_item_receipt r JOIN eye.capture_batch b USING (batch_id)
              WHERE r.media_item_id = m.media_item_id
                AND b.requested_area && ST_MakeEnvelope(:w, :s, :e, :n, 4326))
       END)
  AND (CAST(:items AS text[]) IS NULL
       OR m.source_id || ' ' || m.item_id = ANY(CAST(:items AS text[])))
ORDER BY 1, 2
LIMIT :cap
"""

# Items a batch delivered that have, or had, any version in view.
TOUCHED_ITEMS = """
SELECT DISTINCT m.source_id, m.item_id
FROM eye.media_item_receipt r JOIN eye.media_item m USING (media_item_id)
WHERE r.batch_id = CAST(:batch AS uuid)
  AND EXISTS (
    SELECT 1 FROM eye.media_item o
    WHERE o.source_id = m.source_id AND o.item_id = m.item_id
      AND o.first_published_time >= :start AND o.first_published_time < :end
      AND (o.place IS NULL OR ST_Intersects(o.place, ST_MakeEnvelope(:w, :s, :e, :n, 4326))))
ORDER BY 1, 2
LIMIT :cap
"""

ITEM_VERSIONS = """
SELECT * FROM (
  SELECT m.media_item_id::text, m.source_id, m.item_id, m.kind, m.status,
         eye.iso_utc(m.first_published_time), eye.iso_utc(m.revision_time),
         eye.iso_utc(v.first_received_time), m.url, m.syndicated_from, m.publisher, m.creator,
         m.headline, m.language, m.rights_status, m.licence, m.attribution,
         eye.iso_utc(m.capture_time_claimed), m.place_role, m.place_method, ST_X(m.place),
         ST_Y(m.place), m.place_precision_m, v.version, v.is_current,
         ARRAY(SELECT r.batch_id::text FROM eye.media_item_receipt r
               WHERE r.media_item_id = m.media_item_id ORDER BY r.batch_id::text
               LIMIT :evidence_cap),
         row_number() OVER (PARTITION BY m.source_id, m.item_id
                            ORDER BY v.version, m.media_item_id) AS rank
  FROM eye.media_item m JOIN eye.media_item_version v USING (media_item_id)
  WHERE m.source_id || ' ' || m.item_id = ANY(CAST(:keys AS text[]))
) ranked
WHERE rank <= :version_cap
ORDER BY 2, 3, 24, 1
"""


def _t(text: str | None) -> str | None:
    return None if text is None else text.replace(".000000Z", "Z")


def _version(row: tuple) -> dict:
    (
        vid,
        _source,
        _item,
        _kind,
        status,
        first,
        revision,
        received,
        url,
        syndicated,
        publisher,
        creator,
        headline,
        language,
        rights,
        licence,
        attribution,
        captured,
        role,
        method,
        lon,
        lat,
        precision,
        version,
        current,
        batches,
        _rank,
    ) = row
    withheld = rights == "unknown"
    out = {
        "version_id": vid,
        "version": int(version),
        "is_current": current,
        "status": status,
        "first_published_time": _t(first),
        "revision_time": _t(revision),
        "received_time": _t(received),
        "evidence_batch_ids": list(batches),
        "url": url,
        "syndicated_from": syndicated,
        "publisher": publisher,
        "creator": creator,
        # Unknown reuse rights: a link only, never the source's words.
        "headline": None if withheld else headline,
        "headline_withheld": withheld,
        "language": language,
        "rights": {"status": rights}
        if licence is None
        else {"status": rights, "licence": licence, "attribution": attribution},
        "capture_time_claimed": _t(captured),
        "place": None
        if role is None
        else {
            "role": role,
            "method": method,
            "coords": [float(lon), float(lat)],
            "precision_m": float(precision),
        },
    }
    return out


def items(conn, params: dict, limits, keys: list[str] | None, labels: dict) -> list[dict]:
    """Media items in view (or only ``keys``: "source item" strings), each with every version."""
    cap = min(limits.max_media, 500)
    rows = conn.run(ITEMS_IN_VIEW, **params, items=keys, cap=cap + 1)
    if len(rows) > cap:
        raise MediaRefused(413, f"more than {cap} news and media items; narrow the request")
    if not rows:
        return []
    found = [f"{source} {item}" for source, item in rows]
    versions = conn.run(
        ITEM_VERSIONS,
        keys=found,
        evidence_cap=MAX_EVIDENCE + 1,
        version_cap=MAX_ITEM_VERSIONS + 1,
    )
    grouped: dict[tuple[str, str], list[tuple]] = {}
    for row in versions:
        grouped.setdefault((row[1], row[2]), []).append(row)
    out = []
    for (source, item), group in sorted(grouped.items()):
        if len(group) > MAX_ITEM_VERSIONS:
            raise MediaRefused(
                413, f"item {item} has more than {MAX_ITEM_VERSIONS} versions; not served"
            )
        if any(len(row[25]) > MAX_EVIDENCE for row in group):
            raise MediaRefused(
                413, f"item {item} has a version with more than {MAX_EVIDENCE} deliveries"
            )
        served = [_version(row) for row in group]
        current = [v for v in served if v["is_current"] is True]
        chosen = current[0] if current else None
        out.append(
            {
                "id": media_id(source, item),
                "source": source,
                "source_label": labels.get(source, source),
                "item_id": item,
                "kind": group[0][3],
                "standing": "unresolved" if chosen is None else chosen["status"],
                "current_version_id": None if chosen is None else chosen["version_id"],
                "versions": served,
            }
        )
    return out


def touched(conn, params: dict, batch_id: str, limit: int) -> list[tuple]:
    return conn.run(TOUCHED_ITEMS, **params, batch=batch_id, cap=limit + 1)


def latest(item: dict) -> list[dict]:
    """The current version, or every version of a conflicting latest tier."""
    return [v for v in item["versions"] if v["is_current"] is not False]


def items_in_view_from(media: list[dict], batch_id: str) -> int:
    """Items in this list whose current (or conflicting latest) version came,
    possibly among others, from ``batch_id``."""
    return sum(1 for m in media if any(batch_id in v["evidence_batch_ids"] for v in latest(m)))


def reporting_batches(media: list[dict], every_version: bool = False) -> set[str]:
    return {
        batch
        for m in media
        for v in m["versions"]
        if every_version or v["is_current"] is not False
        for batch in v["evidence_batch_ids"]
    }


# --- suggestions ------------------------------------------------------------------------


def _distance_m(a: list[float], b: list[float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(h)))


def _seconds(a: str, b: str) -> float:
    from datetime import datetime

    return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds())


def _syndicated(a: dict, b: dict) -> bool:
    return (
        (a["syndicated_from"] is not None and a["syndicated_from"] == b["url"])
        or (b["syndicated_from"] is not None and b["syndicated_from"] == a["url"])
        or (a["syndicated_from"] is not None and a["syndicated_from"] == b["syndicated_from"])
    )


def _stated_site(v: dict) -> bool:
    place = v["place"]
    if place is None:
        return False
    return place["role"] == "event_place" and place["method"] == "source_stated"


def suggestions(media: list[dict]) -> list[dict]:
    """Deterministic pairwise suggestions between items; never a merge.

    * ``syndicated_copy``: one item names the other as its original, or both
      name the same original. They are **not** independent reports.
    * ``place_and_time``: different publishers, both with a source-stated
      event place, the two places within their combined stated precision,
      first published within six hours of each other. They **may** describe
      the same story; nothing here says they do.

    Only items with one current version take part; headlines are never read.
    """
    current = []
    for m in media:
        versions = [v for v in m["versions"] if v["is_current"] is True]
        if versions and versions[0]["status"] != "retracted":
            current.append((m["id"], versions[0]))
    out = []
    for i, (id_a, a) in enumerate(current):
        for id_b, b in current[i + 1 :]:
            pair = sorted([id_a, id_b])
            if _syndicated(a, b):
                out.append({"items": pair, "basis": "syndicated_copy", "status": "suggestion"})
            elif (
                a["publisher"].casefold() != b["publisher"].casefold()
                and _stated_site(a)
                and _stated_site(b)
                and _distance_m(a["place"]["coords"], b["place"]["coords"])
                <= a["place"]["precision_m"] + b["place"]["precision_m"]
                and _seconds(a["first_published_time"], b["first_published_time"])
                <= SUGGESTION_WINDOW_S
            ):
                out.append({"items": pair, "basis": "place_and_time", "status": "suggestion"})
    out.sort(key=lambda s: (s["items"], s["basis"]))
    return out[:MAX_SUGGESTIONS]
