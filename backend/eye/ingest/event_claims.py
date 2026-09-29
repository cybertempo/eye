"""Event claims: the source-independent part of the event ledger (Package 4c).

A claim is one version of one source's case, as the source published it. Any
adapter (today only the synthetic ``synthetic-events`` source) maps its
provider's shape to the claim fields below; everything after that (ids,
versions, corrections, conflicts, links to tracks) is the same for every
source.

Rules enforced here and again by migration 0005:

* The only geometry in a claim is the **reported event location**, with its
  type (point, road segment with direction, or area) and stated precision. A
  last observed position comes from observations when a claim is shown.
* A claim whose basis is motion inference (lost signal, AIS gap, stopped
  vessel, traffic slowdown) is a **review candidate** and nothing more. An
  accident, casualty or collision needs a sourced report citing evidence.
* Publication time orders corrections; EYE receipt time is stamped by EYE.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

LAYER_KINDS = {
    "flight": frozenset(
        {
            "aviation_accident",
            "aviation_incident",
            "emergency_declared",
            "diversion",
            "flight_arrival",
            "signal_lost",
        }
    ),
    "vessel": frozenset(
        {"marine_casualty", "vessel_distress", "vessel_port_arrival", "ais_gap", "vessel_stopped"}
    ),
    "road": frozenset(
        {"road_collision", "road_incident", "road_closure", "road_congestion", "traffic_slowdown"}
    ),
}
# Kinds that motion data alone can suggest. They are review candidates only.
CANDIDATE_KINDS = frozenset({"signal_lost", "ais_gap", "vessel_stopped", "traffic_slowdown"})
# Kinds that describe harm; only a sourced report can carry them.
ACCIDENT_KINDS = frozenset({"aviation_accident", "marine_casualty", "road_collision"})
REPORT_BASES = frozenset({"official_report", "operator_report"})
BASES = REPORT_BASES | {"motion_inference"}
REPORT_STATUSES = frozenset({"reported", "preliminary", "final", "retracted", "cleared"})
LOCATION_TYPES = frozenset({"point", "segment", "area"})
MAX_SEGMENT_POINTS = 100
MAX_AREA_POINTS = 100
MAX_IDENTIFIERS = 10
MAX_UNCERTAINTY_S = 30 * 24 * 3600
CLAIM_FIELDS = frozenset(
    {
        "case_id",
        "basis",
        "kind",
        "status",
        "event_time",
        "event_time_uncertainty_s",
        "source_published_time",
        "location",
        "evidence_ref",
        "subject",
        "summary",
    }
)
SUBJECT_SCHEMES = frozenset({"track", "registration", "callsign", "mmsi", "imo", "plate"})


@dataclass(frozen=True)
class Location:
    type: str
    coords: tuple  # point: (lon, lat); segment/area: ((lon, lat), ...)
    precision_m: float
    direction: str | None  # segments only: "forward" (as drawn) or "both"

    def wkt(self) -> str:
        def pair(p):
            return f"{p[0]!r} {p[1]!r}"

        if self.type == "point":
            return f"POINT({pair(self.coords)})"
        if self.type == "segment":
            return f"LINESTRING({', '.join(pair(p) for p in self.coords)})"
        return f"POLYGON(({', '.join(pair(p) for p in self.coords)}))"

    def as_json(self) -> dict:
        coords = list(self.coords) if self.type == "point" else [list(p) for p in self.coords]
        out = {"type": self.type, "coords": coords, "precision_m": self.precision_m}
        if self.direction is not None:
            out["direction"] = self.direction
        return out


@dataclass(frozen=True)
class Claim:
    case_id: str
    basis: str
    kind: str
    status: str
    event_time: datetime | None
    event_time_uncertainty_s: int | None
    source_published_time: datetime
    location: Location
    evidence_ref: str | None
    subject_identifiers: tuple[str, ...]
    subject_window: tuple[datetime, datetime] | None
    summary: str | None

    def content(self, iso) -> dict:
        """Everything the source said in this version; EYE receipt time is excluded."""
        return {
            "basis": self.basis,
            "kind": self.kind,
            "status": self.status,
            "event_time": None if self.event_time is None else iso(self.event_time),
            "event_time_uncertainty_s": self.event_time_uncertainty_s,
            "source_published_time": iso(self.source_published_time),
            "location": self.location.as_json(),
            "evidence_ref": self.evidence_ref,
            "subject_identifiers": sorted(self.subject_identifiers),
            "subject_window": None
            if self.subject_window is None
            else [iso(self.subject_window[0]), iso(self.subject_window[1])],
            "summary": self.summary,
        }

    def content_sha256(self, iso) -> str:
        canonical = json.dumps(self.content(iso), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _number(value: object, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{what} must be a number from {low} to {high}")
    return float(value)


def _position(value: object, what: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{what} must be [longitude, latitude]")
    return (
        _number(value[0], f"{what} longitude", -180, 180),
        _number(value[1], f"{what} latitude", -90, 90),
    )


def parse_location(raw: object, what: str) -> Location:
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be an object")
    kind = raw.get("type")
    if kind not in LOCATION_TYPES:
        raise ValueError(f"{what}.type must be one of {sorted(LOCATION_TYPES)}")
    allowed = {"type", "coords", "precision_m"} | ({"direction"} if kind == "segment" else set())
    if set(raw) - allowed:
        raise ValueError(f"{what} has unexpected fields {sorted(set(raw) - allowed)}")
    precision = _number(raw.get("precision_m"), f"{what}.precision_m", 0, 1_000_000)
    coords = raw.get("coords")
    direction = None
    if kind == "point":
        points: tuple = _position(coords, f"{what}.coords")
    else:
        limit = MAX_SEGMENT_POINTS if kind == "segment" else MAX_AREA_POINTS
        if not isinstance(coords, list) or not 2 <= len(coords) <= limit:
            raise ValueError(f"{what}.coords must hold 2 to {limit} positions")
        points = tuple(_position(p, f"{what}.coords[{i}]") for i, p in enumerate(coords))
        if kind == "segment":
            direction = raw.get("direction")
            if direction not in ("forward", "both"):
                raise ValueError(f"{what}.direction must be forward or both")
        elif len(points) < 4 or points[0] != points[-1]:
            raise ValueError(f"{what} must be a closed ring of at least 4 positions")
    return Location(kind, points, precision, direction)


def parse_claim(raw: object, index: int, layer: str, received: datetime, parse_time) -> Claim:
    """One claim as the adapter mapped it. Raises ValueError with the reason."""
    if not isinstance(raw, dict):
        raise ValueError(f"claim {index} is not an object")
    if "received_time" in raw:
        raise ValueError(f"claim {index} supplies received_time; receipt time is stamped by EYE")
    if set(raw) - CLAIM_FIELDS:
        raise ValueError(f"claim {index} has unexpected fields {sorted(set(raw) - CLAIM_FIELDS)}")
    from eye.ingest.capture import IDENTIFIER

    case_id = raw.get("case_id")
    if not isinstance(case_id, str) or not IDENTIFIER.match(case_id):
        raise ValueError(f"claim {index} has an invalid case_id")
    basis, kind, status = raw.get("basis"), raw.get("kind"), raw.get("status")
    if basis not in BASES:
        raise ValueError(f"claim {index} basis must be one of {sorted(BASES)}")
    if kind not in LAYER_KINDS[layer]:
        raise ValueError(f"claim {index} kind {kind!r} is not a {layer} event kind")
    evidence = raw.get("evidence_ref")
    if basis == "motion_inference":
        # Motion data can only raise a question, never answer it.
        if kind not in CANDIDATE_KINDS:
            raise ValueError(
                f"claim {index}: motion inference cannot report {kind}; "
                "it can only be a review candidate"
            )
        if status != "candidate":
            raise ValueError(f"claim {index}: motion inference must have status candidate")
        if evidence is not None:
            raise ValueError(f"claim {index}: motion inference cites no source report")
    else:
        if kind in CANDIDATE_KINDS:
            raise ValueError(f"claim {index}: {kind} is a motion candidate, not a report kind")
        if status not in REPORT_STATUSES:
            raise ValueError(f"claim {index} status must be one of {sorted(REPORT_STATUSES)}")
        if not isinstance(evidence, str) or not 1 <= len(evidence) <= 200:
            raise ValueError(f"claim {index}: a sourced report must cite evidence_ref")
    published = parse_time(raw.get("source_published_time"), f"claim {index} source_published_time")
    if received < published:
        raise ValueError(f"claim {index} was published after EYE received the response")
    event_time = uncertainty = None
    if raw.get("event_time") is not None:
        event_time = parse_time(raw["event_time"], f"claim {index} event_time")
        uncertainty = raw.get("event_time_uncertainty_s")
        if (
            isinstance(uncertainty, bool)
            or not isinstance(uncertainty, int)
            or not 0 <= uncertainty <= MAX_UNCERTAINTY_S
        ):
            raise ValueError(
                f"claim {index} event_time_uncertainty_s must be 0..{MAX_UNCERTAINTY_S}"
            )
        if (published - event_time).total_seconds() < -uncertainty:
            raise ValueError(f"claim {index} was published before the event could have happened")
    elif raw.get("event_time_uncertainty_s") is not None:
        raise ValueError(f"claim {index} gives an uncertainty without an event_time")
    location = parse_location(raw.get("location"), f"claim {index} location")
    identifiers: tuple[str, ...] = ()
    window = None
    subject = raw.get("subject")
    if subject is not None:
        if not isinstance(subject, dict) or set(subject) - {"identifiers", "window"}:
            raise ValueError(f"claim {index} subject must be {{identifiers, window}}")
        ids = subject.get("identifiers", [])
        if not isinstance(ids, list) or len(ids) > MAX_IDENTIFIERS:
            raise ValueError(f"claim {index} subject.identifiers: at most {MAX_IDENTIFIERS}")
        for item in ids:
            scheme, _, value = item.partition(":") if isinstance(item, str) else ("", "", "")
            if scheme not in SUBJECT_SCHEMES or not value or not IDENTIFIER.match(item):
                raise ValueError(f"claim {index} has an invalid subject identifier {item!r}")
        identifiers = tuple(ids)
        if subject.get("window") is not None:
            w = subject["window"]
            if not isinstance(w, dict) or set(w) != {"start", "end"}:
                raise ValueError(f"claim {index} subject.window must be {{start, end}}")
            start = parse_time(w["start"], f"claim {index} subject.window.start")
            end = parse_time(w["end"], f"claim {index} subject.window.end")
            if end <= start:
                raise ValueError(f"claim {index} subject.window must end after it starts")
            window = (start, end)
    summary = raw.get("summary")
    if summary is not None and (not isinstance(summary, str) or not 1 <= len(summary) <= 500):
        raise ValueError(f"claim {index} summary must be 1..500 characters")
    return Claim(
        case_id=case_id,
        basis=basis,
        kind=kind,
        status=status,
        event_time=event_time,
        event_time_uncertainty_s=uncertainty,
        source_published_time=published,
        location=location,
        evidence_ref=evidence,
        subject_identifiers=identifiers,
        subject_window=window,
        summary=summary,
    )
