"""Event-ledger cases for the browser API (Package 4c).

Reads ``eye.event_claim`` and its version view and returns ``EventCase``
dicts. Rules, all conservative:

* Every version of a case is returned (corrections, retractions, conflicting
  claims), each with its own reported location, precision and evidence.
* ``standing`` comes only from the current version: a sourced report, a
  review candidate (motion data), retracted, or unresolved when the latest
  versions conflict. Motion data never becomes a report here or anywhere.
* A case is linked to a track only when exactly one track named by its
  ``track:`` identifiers has a resolved observation in the time window and was
  last seen close enough to the reported location (precision plus
  ``LINK_ALLOWANCE_M``). Otherwise it stays unlinked with the reason. The
  linked track's last observed position is returned separately from the
  reported location and is never merged with it.
* Where no event-report source covers part of the view, coverage says
  ``unknown``: an absent source is never "no events".
"""

from __future__ import annotations

import hashlib
import json

LINK_ALLOWANCE_M = 20_000
MAX_CASE_CLAIMS = 100  # wire schema EventCase.claims maxItems
MAX_CLAIM_EVIDENCE = 100  # wire schema evidence_batch_ids maxItems
MAX_CANDIDATES = 10  # wire schema candidate_track_ids maxItems
EVENT_METRIC = "event_reports"  # stored per batch: distinct cases in the whole capture
# Served instead of the stored metric: cases in this view (the event list's own
# area, occurrence-interval and version rules) whose current or conflicting
# latest report came from that batch. The stored batch metric is unchanged.
EVENT_VIEW_METRIC = "event_cases_in_view"
NO_SOURCE = "no event-report source covers this area and time; events unknown, not absent"


class EventsRefused(ValueError):
    """Raised with a status and reason; feed.py turns it into QueryRefused."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


def case_id(source: str, layer: str, case: str) -> str:
    digest = hashlib.sha256(json.dumps([source, layer, case]).encode("utf-8")).hexdigest()
    return f"evt-{digest[:32]}"


# When a case may have happened: its possible occurrence interval is the
# nominal event time plus or minus its stated uncertainty, and a case is
# selected when that interval overlaps the requested [start, end), so a case
# whose nominal time lies just outside the view but may have happened inside
# it is included. A claim with no event time falls back to its publication
# time. The nominal time and uncertainty are served unchanged. The same
# condition is written out in CASES_IN_VIEW and TOUCHED_CASES.


# A case is in view when a version that is current, or part of a conflicting
# latest tier, lies in the area and may have happened in the interval.
CASES_IN_VIEW = """
SELECT DISTINCT c.source_id, c.layer, c.case_id
FROM eye.event_claim c JOIN eye.event_claim_version v USING (claim_id)
WHERE v.is_current IS NOT FALSE
  AND c.layer = ANY(CAST(:layers AS text[]))
  AND (CASE WHEN c.event_time IS NULL
            THEN c.source_published_time >= :start AND c.source_published_time < :end
            ELSE c.event_time - make_interval(secs => c.event_time_uncertainty_s) < :end
             AND c.event_time + make_interval(secs => c.event_time_uncertainty_s) >= :start
       END)
  AND ST_Intersects(c.location, ST_MakeEnvelope(:w, :s, :e, :n, 4326))
  AND (CAST(:cases AS text[]) IS NULL
       OR c.source_id || ' ' || c.layer || ' ' || c.case_id = ANY(CAST(:cases AS text[])))
ORDER BY 1, 2, 3
LIMIT :cap
"""

# Cases an event batch touched that have, or had, any version in view.
TOUCHED_CASES = """
SELECT DISTINCT c.source_id, c.layer, c.case_id
FROM eye.event_claim_receipt r JOIN eye.event_claim c USING (claim_id)
WHERE r.batch_id = CAST(:batch AS uuid)
  AND c.layer = ANY(CAST(:layers AS text[]))
  AND EXISTS (
    SELECT 1 FROM eye.event_claim v
    WHERE v.source_id = c.source_id AND v.layer = c.layer AND v.case_id = c.case_id
      AND (CASE WHEN v.event_time IS NULL
            THEN v.source_published_time >= :start AND v.source_published_time < :end
            ELSE v.event_time - make_interval(secs => v.event_time_uncertainty_s) < :end
             AND v.event_time + make_interval(secs => v.event_time_uncertainty_s) >= :start
           END)
      AND ST_Intersects(v.location, ST_MakeEnvelope(:w, :s, :e, :n, 4326)))
ORDER BY 1, 2, 3
LIMIT :cap
"""

CASE_CLAIMS = """
SELECT * FROM (
  SELECT c.claim_id::text, c.source_id, c.layer, c.case_id, c.basis::text, c.kind,
         c.status::text, eye.iso_utc(c.event_time), c.event_time_uncertainty_s,
         eye.iso_utc(c.source_published_time), eye.iso_utc(v.first_received_time),
         c.location_type,
         (SELECT array_agg(ARRAY[ST_X(d.geom), ST_Y(d.geom)] ORDER BY d.path)
          FROM ST_DumpPoints(c.location) d),
         c.precision_m, c.segment_direction, c.evidence_ref, c.subject_identifiers,
         eye.iso_utc(c.subject_window_start), eye.iso_utc(c.subject_window_end), c.summary,
         v.version, v.is_current,
         ARRAY(SELECT r.batch_id::text FROM eye.event_claim_receipt r
               WHERE r.claim_id = c.claim_id ORDER BY r.batch_id::text LIMIT :evidence_cap),
         row_number() OVER (PARTITION BY c.source_id, c.layer, c.case_id
                            ORDER BY v.version, c.claim_id) AS claim_rank
  FROM eye.event_claim c JOIN eye.event_claim_version v USING (claim_id)
  WHERE c.source_id || ' ' || c.layer || ' ' || c.case_id = ANY(CAST(:keys AS text[]))
) ranked
WHERE claim_rank <= :claim_cap
ORDER BY 2, 3, 4, 21, 1
"""

# The last resolved position of one track in [start, until].
LAST_OBSERVED = """
SELECT o.observation_id::text, eye.iso_utc(o.observed_time), eye.iso_utc(v.first_received_time),
       ST_X(o.position), ST_Y(o.position), o.altitude_m,
       ST_Distance(o.position::geography, c.location::geography)
FROM eye.observation o
JOIN eye.observation_version v USING (observation_id)
JOIN eye.event_claim c ON c.claim_id = CAST(:claim AS uuid)
WHERE v.is_current IS TRUE
  AND o.source_id = :source AND o.layer = :layer AND o.source_record_id = :record
  AND o.observed_time >= :start AND o.observed_time <= :until
ORDER BY o.observed_time DESC, o.observation_id
LIMIT 1
"""


def _t(text: str | None) -> str | None:
    return None if text is None else text.replace(".000000Z", "Z")


def _location(kind: str, points: list, precision: float, direction: str | None) -> dict:
    coords = [[float(x), float(y)] for x, y in points]
    out: dict = {
        "type": kind,
        "coords": coords[0] if kind == "point" else coords,
        "precision_m": float(precision),
    }
    if kind == "segment":
        out["direction"] = direction
    return out


def _claim(row) -> dict:
    (
        cid, _source, _layer, _case, basis, kind, status, event_time, uncertainty, published,
        received, loc_type, points, precision, direction, evidence, identifiers, w_start, w_end,
        summary, version, current, batches, _rank,
    ) = row  # fmt: skip
    if len(batches) > MAX_CLAIM_EVIDENCE:
        raise EventsRefused(413, f"claim {cid} has more than {MAX_CLAIM_EVIDENCE} evidence batches")
    return {
        "claim_id": cid,
        "version": int(version),
        "is_current": current,
        "basis": basis,
        "kind": kind,
        "status": status,
        "event_time": _t(event_time),
        "event_time_uncertainty_s": uncertainty,
        "published_time": _t(published),
        "received_time": _t(received),
        "reported_event_location": _location(loc_type, points, precision, direction),
        "evidence_ref": evidence,
        "evidence_batch_ids": list(batches),
        "subject_identifiers": sorted(identifiers or []),
        "subject_window": None if w_start is None else {"start": _t(w_start), "end": _t(w_end)},
        "summary": summary,
    }


def _standing(current: dict | None) -> str:
    if current is None:
        return "unresolved"
    if current["status"] == "retracted":
        return "retracted"
    if current["basis"] == "motion_inference":
        return "review_candidate"
    return "report"


def _track_targets(identifiers: list[str], layer: str) -> list[tuple[str, str, str]]:
    targets = []
    for item in identifiers:
        scheme, _, value = item.partition(":")
        if scheme != "track":
            continue
        parts = value.split(":", 2)
        if len(parts) == 3 and parts[1] == layer:
            targets.append((parts[0], parts[1], parts[2]))
    return targets


def _link(
    conn, layer: str, current: dict | None, row_claim_id: str | None
) -> tuple[dict, dict | None]:
    from eye.api.feed import iso_parse, track_id

    def none(state: str, reason: str, candidates=()) -> tuple[dict, None]:
        return (
            {
                "state": state,
                "track_id": None,
                "reason": reason,
                "candidate_track_ids": list(candidates)[:MAX_CANDIDATES],
            },
            None,
        )

    if current is None:
        return none("unlinked", "the latest claims conflict; not linked to any track")
    targets = _track_targets(current["subject_identifiers"], layer)
    if not targets:
        return none("not_applicable", "the claim names no EYE track")
    window = current["subject_window"]
    if window is None:
        return none("unlinked", "the claim gives no time window to match a track against")
    start = iso_parse(window["start"])
    until = iso_parse(window["end"])
    if current["event_time"] is not None:
        latest = iso_parse(current["event_time"])
        from datetime import timedelta

        latest += timedelta(seconds=current["event_time_uncertainty_s"] or 0)
        until = min(until, latest)
    matches = []
    for source, lay, record in targets[:MAX_CANDIDATES]:
        found = conn.run(
            LAST_OBSERVED,
            claim=row_claim_id,
            source=source,
            layer=lay,
            record=record,
            start=start,
            until=until,
        )
        if found:
            matches.append((source, lay, record, found[0]))
    candidates = [track_id(s, lay, r) for s, lay, r, _ in matches]
    if not matches:
        return none(
            "unlinked", "no observed track matches the named identifiers in the time window"
        )
    if len(matches) > 1:
        reason = f"{len(matches)} tracks match the named identifiers; not linked"
        return none("ambiguous", reason, candidates)
    source, lay, record, (oid, observed, received, lon, lat, alt, distance) = matches[0]
    allowance = current["reported_event_location"]["precision_m"] + LINK_ALLOWANCE_M
    if float(distance) > allowance:
        return none(
            "unlinked",
            f"the only matching track was last observed {float(distance) / 1000:.1f} km from the "
            f"reported location, beyond its precision plus {LINK_ALLOWANCE_M // 1000} km; "
            "not linked",
            candidates,
        )
    tid = candidates[0]
    return (
        {
            "state": "linked",
            "track_id": tid,
            "reason": (
                "one track matches the named identifier, time window and location (last observed "
                f"{float(distance) / 1000:.1f} km from the reported location)"
            ),
            "candidate_track_ids": candidates,
        },
        {
            "track_id": tid,
            "observation_id": oid,
            "source": source,
            "source_record_id": record,
            "observed_time": _t(observed),
            "received_time": _t(received),
            "lon": float(lon),
            "lat": float(lat),
            "alt_m": None if alt is None else float(alt),
        },
    )


def cases(conn, params: dict, limits, keys: list[str] | None, labels: dict) -> list[dict]:
    """Event cases in view (or, with ``keys``, only those cases, if still in view)."""
    cap = min(limits.max_events, 1000)
    rows = conn.run(CASES_IN_VIEW, **params, cases=keys, cap=cap + 1)
    if len(rows) > cap:
        raise EventsRefused(413, f"more than {cap} event cases; narrow the request")
    if not rows:
        return []
    in_view = [f"{s} {lay} {c}" for s, lay, c in rows]
    claims = conn.run(
        CASE_CLAIMS,
        keys=in_view,
        claim_cap=MAX_CASE_CLAIMS + 1,
        evidence_cap=MAX_CLAIM_EVIDENCE + 1,
    )
    grouped: dict[tuple, list] = {}
    for row in claims:
        grouped.setdefault((row[1], row[2], row[3]), []).append(row)
    out = []
    for key in sorted(grouped):
        source, layer, case = key
        versions = grouped[key]
        if len(versions) > MAX_CASE_CLAIMS:
            raise EventsRefused(
                413,
                f"{layer} case {case} from {source} has more than {MAX_CASE_CLAIMS} versions; "
                f"one message carries at most {MAX_CASE_CLAIMS} and none is dropped",
            )
        claims_out = [_claim(r) for r in versions]
        current = [c for c in claims_out if c["is_current"] is True]
        chosen = current[0] if len(current) == 1 else None
        link, last = _link(conn, layer, chosen, chosen["claim_id"] if chosen else None)
        out.append(
            {
                "id": case_id(source, layer, case),
                "layer": layer,
                "source": source,
                "source_label": labels.get(source, f"Source {source}"),
                "case_id": case,
                "standing": _standing(chosen),
                "current_claim_id": None if chosen is None else chosen["claim_id"],
                "claims": claims_out,
                "link": link,
                "last_observed_position": last,
            }
        )
    return out


def coverage_gaps(full_spans: list[tuple], layers, start: str, end: str) -> list[dict]:
    """Unknown event coverage for every part of the view's interval that no
    event source covered over the whole view area.

    ``full_spans`` holds (layer, start, end) of event coverage rows whose
    source area covers the entire view. A source covering only part of the
    view closes no gap: its row is shown as partial, and the view stays
    unknown for that time.
    """
    gaps = []
    for layer in layers:
        spans = sorted((a, b) for lay, a, b in full_spans if lay == layer)
        cursor = start
        for a, b in spans:
            if _key(a) > _key(cursor):
                gaps.append(_gap(layer, cursor, min(a, end, key=_key)))
            if _key(b) > _key(cursor):
                cursor = b
            if _key(cursor) >= _key(end):
                break
        if _key(cursor) < _key(end):
            gaps.append(_gap(layer, cursor, end))
    return gaps


def cases_in_view_from(events: list[dict], batch_id: str) -> int:
    """Cases in this event list whose current (or conflicting latest) claim
    came, possibly among others, from ``batch_id``."""
    count = 0
    for event in events:
        latest = [c for c in event["claims"] if c["is_current"] is not False]
        if any(batch_id in c["evidence_batch_ids"] for c in latest):
            count += 1
    return count


def clip(interval_start: str, interval_end: str, start: str, end: str) -> tuple[str, str]:
    """The part of [interval_start, interval_end) inside the view's [start, end)."""
    return max(interval_start, start, key=_key), min(interval_end, end, key=_key)


def _key(time: str) -> str:
    # Wire times: fixed-width fields, 0-6 fractional digits; pad for ordering.
    head, _, frac = time[:-1].partition(".")
    return f"{head}.{frac.ljust(6, '0')}"


def _gap(layer: str, start: str, end: str) -> dict:
    return {
        "layer": layer,
        "interval": {"start": start, "end": end},
        "state": "unknown",
        "reason": NO_SOURCE,
        "metric": {"name": EVENT_VIEW_METRIC, "value": None},
    }
