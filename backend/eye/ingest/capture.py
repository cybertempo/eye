"""Capture pipeline: archive raw evidence, then commit parsed records.

1. ``archive`` stores the exact bytes and a ``pending`` batch in one transaction.
2. ``commit_batch`` re-reads those stored bytes (never the caller's copy),
   checks the checksum, derives observations, receipts and coverage, and
   writes them in a second transaction, then marks the batch ``committed``.

A crash between the steps leaves a discoverable pending batch that
``replay_pending`` completes. ``derive`` is the single derivation used both to
commit and to verify, so ``verify_replay`` can compare every stored value.

Time provenance (three separate facts, never substituted for each other):

* ``observed_time`` - source event time, from the provider record.
* ``source_published_time`` - when the source issued that version of the
  record, from the provider record; it orders corrections.
* ``received_time`` - EYE receipt time, stamped by EYE's capture adapter in the
  capture envelope (``attempt.finished_at``: when the provider response
  arrived). A provider record that carries its own receipt time is rejected.

Chronology enforced per record: observed <= published <= received, observed
inside the requested interval. Only sources with an approved row in
docs/source-policy-register.md are accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from eye.ingest import event_claims, media_items, synthetic_ais
from eye.storage.db import Connection, transaction

CAPTURE_FORMAT = "eye.synthetic-capture/2"
EVENT_CAPTURE_FORMAT = "eye.synthetic-event-claims/1"
MEDIA_CAPTURE_FORMAT = "eye.synthetic-media-items/1"
# The wire version whose observation fields each receipt was recorded under.
# eye.wire/2 and eye.wire/3 changed only how tracks, counts and events are
# served, not an observation's fields, so receipts keep this label and replay
# stays exact.
RECEIPT_SCHEMA_VERSION = "eye.wire/1"
APPROVED_SOURCES = frozenset(
    {"synthetic-fixture", "synthetic-ais", "synthetic-events", "synthetic-news"}
)
# News and media providers on the Package 4d shortlist: known, reviewed as
# candidates only, and refused until their register row is approved.
CANDIDATE_SOURCES = frozenset(
    {
        "gdelt",
        "guardian-open-platform",
        "nytimes",
        "newsapi",
        "youtube-data",
        "wikimedia-commons",
        "flickr",
        "google-news",
    }
)
# Record parser per approved source; the AIS adapter maps its own message shape.
SOURCE_LAYERS = {
    "synthetic-fixture": frozenset({"flight", "vessel", "road"}),
    "synthetic-ais": frozenset({"vessel"}),
    "synthetic-events": frozenset({"flight", "vessel", "road"}),
    "synthetic-news": frozenset({"news"}),
}
# The capture format each source delivers: positions, or event claims.
SOURCE_FORMATS = {
    "synthetic-fixture": CAPTURE_FORMAT,
    "synthetic-ais": CAPTURE_FORMAT,
    "synthetic-events": EVENT_CAPTURE_FORMAT,
    "synthetic-news": MEDIA_CAPTURE_FORMAT,
}
EVENT_METRIC_NAME = "event_reports"
MEDIA_METRIC_NAME = "media_items"
MEDIA_LAYER = "news"
LAYERS = frozenset({"flight", "vessel", "road", MEDIA_LAYER})  # every layer any source may use
PROVIDER_STATUSES = frozenset({"ok", "error", "timeout", "indeterminate"})
MAX_EVIDENCE_BYTES = 1_048_576
MAX_RECORDS = 10_000
MAX_CLAIMS = 1_000
MAX_ITEMS = 1_000
DERIVATION_VERSION = "coverage/2"
METRIC_NAME = "tracks_observed"
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://eye.invalid/ids/v1")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TIMESTAMP = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(\.[0-9]{1,6})?Z\Z"
)
RECORD_FIELDS = frozenset(
    {
        "record_id",
        "observed_time",
        "source_published_time",
        "lon",
        "lat",
        "alt_m",
        "confidence",
        "quality_flags",
    }
)


class CaptureRejected(ValueError):
    """The capture envelope is unusable; nothing was archived."""


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, "|".join(parts)))


def parse_time(value: object, what: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP.match(value):
        raise ValueError(f"{what} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError as exc:  # e.g. 2026-02-30
        raise ValueError(f"{what} is not a real calendar date and time") from exc


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _number(value: object, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{what} must be a number from {low} to {high}")
    return float(value)


@dataclass(frozen=True)
class Record:
    source_record_id: str
    observed_time: datetime
    source_published_time: datetime
    lon: float
    lat: float
    alt_m: float | None
    confidence: float | None
    quality_flags: tuple[str, ...]

    @property
    def content_sha256(self) -> str:
        # EYE receipt time is excluded: a duplicate delivery of the same source
        # version is the same observation with a second receipt.
        content = {
            "alt_m": self.alt_m,
            "confidence": self.confidence,
            "lat": self.lat,
            "lon": self.lon,
            "quality_flags": sorted(self.quality_flags),
            "source_published_time": iso(self.source_published_time),
        }
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ParsedCapture:
    sha256: str
    size: int
    source_id: str
    layer: str
    adapter_version: str
    bbox: tuple[float, float, float, float]
    requested_start: datetime
    requested_end: datetime
    expected_interval_s: int
    started_at: datetime
    finished_at: datetime
    provider_status: str
    quota_cost: float
    observed_start: datetime | None
    observed_end: datetime | None
    capture_format: str = CAPTURE_FORMAT
    submitted: int = 0
    records: list[Record] = field(default_factory=list)
    claims: list[event_claims.Claim] = field(default_factory=list)
    items: list[media_items.Item] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def metric_name(self) -> str:
        return metric_for(self.capture_format)

    @property
    def batch_id(self) -> str:
        return stable_id("batch", self.source_id, self.sha256)

    @property
    def evidence_id(self) -> str:
        return stable_id("evidence", self.sha256)

    @property
    def received_time(self) -> datetime:
        """EYE receipt time for every record in this response."""
        return self.finished_at


def metric_for(capture_format: str) -> str:
    if capture_format == EVENT_CAPTURE_FORMAT:
        return EVENT_METRIC_NAME
    if capture_format == MEDIA_CAPTURE_FORMAT:
        return MEDIA_METRIC_NAME
    return METRIC_NAME


def _parse_record(raw: object, index: int, received: datetime) -> Record:
    if not isinstance(raw, dict):
        raise ValueError(f"record {index} is not an object")
    if "received_time" in raw:
        raise ValueError(
            f"record {index} supplies received_time; receipt time is stamped by EYE, "
            "never by the provider"
        )
    if set(raw) - RECORD_FIELDS:
        raise ValueError(f"record {index} has unexpected fields {sorted(set(raw) - RECORD_FIELDS)}")
    record_id = raw.get("record_id")
    if not isinstance(record_id, str) or not IDENTIFIER.match(record_id):
        raise ValueError(f"record {index} has an invalid record_id")
    flags = raw.get("quality_flags", [])
    if not isinstance(flags, list) or len(flags) > 16:
        raise ValueError(f"record {index} quality_flags must be a list of at most 16")
    if not all(isinstance(f, str) and IDENTIFIER.match(f) for f in flags):
        raise ValueError(f"record {index} has an invalid quality flag")
    observed = parse_time(raw.get("observed_time"), f"record {index} observed_time")
    published = parse_time(
        raw.get("source_published_time"), f"record {index} source_published_time"
    )
    if published < observed:
        raise ValueError(f"record {index} was published before it was observed")
    if received < published:
        raise ValueError(f"record {index} was published after EYE received the response")
    alt = raw.get("alt_m")
    confidence = raw.get("confidence")
    return Record(
        source_record_id=record_id,
        observed_time=observed,
        source_published_time=published,
        lon=_number(raw.get("lon"), f"record {index} lon", -180, 180),
        lat=_number(raw.get("lat"), f"record {index} lat", -90, 90),
        alt_m=None if alt is None else _number(alt, f"record {index} alt_m", -12000, 100000),
        confidence=None if confidence is None else _number(confidence, "confidence", 0, 1),
        quality_flags=tuple(flags),
    )


def parse_capture(raw: bytes) -> ParsedCapture:
    """Parse one synthetic capture. Envelope errors raise; record errors are counted."""
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        raise CaptureRejected(f"capture must be 1..{MAX_EVIDENCE_BYTES} bytes")
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError("capture must be a JSON object")
        if doc.get("synthetic") is not True:
            raise ValueError('only captures marked "synthetic": true are accepted')
        source_id = doc.get("source_id")
        if source_id in CANDIDATE_SOURCES:
            raise ValueError(
                f"source {source_id!r} is a candidate in docs/news-media-source-shortlist.md; "
                "it has no approved row in docs/source-policy-register.md"
            )
        if source_id not in APPROVED_SOURCES:
            raise ValueError(
                f"source {source_id!r} has no approved row in docs/source-policy-register.md"
            )
        capture_format = SOURCE_FORMATS[source_id]
        if doc.get("capture_format") != capture_format:
            raise ValueError(f"capture_format for {source_id} must be {capture_format}")
        layer = doc.get("layer")
        if layer not in LAYERS:
            raise ValueError(f"layer must be one of {sorted(LAYERS)}")
        adapter_version = doc.get("adapter_version")
        if not isinstance(adapter_version, str) or not 1 <= len(adapter_version) <= 64:
            raise ValueError("adapter_version is required")
        request = doc["request"]
        bbox = request["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("request.bbox must be [west, south, east, north]")
        west, east = (_number(bbox[i], "bbox longitude", -180, 180) for i in (0, 2))
        south, north = (_number(bbox[i], "bbox latitude", -90, 90) for i in (1, 3))
        if not (west < east and south < north):
            raise ValueError("request.bbox must have west < east and south < north")
        start = parse_time(request["start"], "request.start")
        end = parse_time(request["end"], "request.end")
        if end <= start:
            raise ValueError("request.end must be after request.start")
        expected = request["expected_interval_s"]
        if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
            raise ValueError("request.expected_interval_s must be a positive integer")
        # The attempt block is written by EYE's capture adapter, not the provider.
        attempt = doc["attempt"]
        status = attempt["provider_status"]
        if status not in PROVIDER_STATUSES:
            raise ValueError(f"attempt.provider_status must be one of {sorted(PROVIDER_STATUSES)}")
        started = parse_time(attempt["started_at"], "attempt.started_at")
        finished = parse_time(attempt["finished_at"], "attempt.finished_at")
        if finished < started:
            raise ValueError("attempt.finished_at is before attempt.started_at")
        if finished < end:
            raise ValueError("EYE cannot receive data for an interval that has not ended")
        quota = _number(attempt["quota_cost"], "attempt.quota_cost", 0, 1e9)
        observed_start = observed_end = None
        if status == "ok":
            observed_start = parse_time(attempt["observed_start"], "attempt.observed_start")
            observed_end = parse_time(attempt["observed_end"], "attempt.observed_end")
            if not start <= observed_start <= observed_end <= end:
                raise ValueError("observed interval must lie inside the requested interval")
        if layer not in SOURCE_LAYERS[source_id]:
            raise ValueError(f"source {source_id} does not provide layer {layer}")
        limit = MAX_RECORDS
        if capture_format == EVENT_CAPTURE_FORMAT:
            records = doc["provider_response"]["claims"]
            limit = MAX_CLAIMS

            def parse_item(item, index, received, layer=layer):
                return event_claims.parse_claim(item, index, layer, received, parse_time)

        elif capture_format == MEDIA_CAPTURE_FORMAT:
            records = doc["provider_response"]["items"]
            limit = MAX_ITEMS
            # Refused before archiving: raw evidence is permanent.
            reason = media_items.unstorable(records)
            if reason is not None:
                raise ValueError(reason)

            def parse_item(item, index, received):
                return media_items.parse_item(item, index, received, parse_time)

        elif source_id == synthetic_ais.SOURCE_ID:
            records = synthetic_ais.messages(doc["provider_response"])
            parse_item = synthetic_ais.parse_message
        else:
            records = doc["provider_response"]["records"]
            parse_item = _parse_record
        if not isinstance(records, list) or len(records) > limit:
            raise ValueError(f"records must be a list of at most {limit}")
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptureRejected(f"unusable capture envelope: {exc}") from exc

    parsed = ParsedCapture(
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
        source_id=source_id,
        layer=layer,
        adapter_version=adapter_version,
        bbox=(west, south, east, north),
        requested_start=start,
        requested_end=end,
        expected_interval_s=expected,
        started_at=started,
        finished_at=finished,
        provider_status=status,
        quota_cost=quota,
        observed_start=observed_start,
        observed_end=observed_end,
        capture_format=capture_format,
        submitted=len(records),
    )
    for index, item in enumerate(records):
        if status != "ok":
            parsed.rejected.append(f"record {index}: provider status {status}")
            continue
        try:
            record = parse_item(item, index, finished)
        except ValueError as exc:
            parsed.rejected.append(str(exc))
            continue
        if isinstance(record, event_claims.Claim):
            # An event source is asked what it published during the request.
            if not start <= record.source_published_time <= end:
                parsed.rejected.append(f"claim {index}: published outside the request")
                continue
            parsed.claims.append(record)
            continue
        if isinstance(record, media_items.Item):
            # A news source is asked what it published or revised during the request.
            if not start <= record.revision_time <= end:
                parsed.rejected.append(f"item {index}: revised outside the request")
                continue
            parsed.items.append(record)
            continue
        if not start <= record.observed_time <= end:
            parsed.rejected.append(f"record {index}: observed_time outside the request")
            continue
        parsed.records.append(record)
    return parsed


def derive_coverage(parsed: ParsedCapture) -> dict:
    """Coverage for one batch. Nothing usable is failed/unknown with no value, never zero."""
    status = parsed.provider_status
    if status in ("error", "timeout"):
        return {"state": "failed", "value": None, "reason": f"provider status {status}"}
    if status == "indeterminate":
        return {"state": "unknown", "value": None, "reason": "provider completeness unknown"}
    usable = {
        EVENT_CAPTURE_FORMAT: parsed.claims,
        MEDIA_CAPTURE_FORMAT: parsed.items,
    }.get(parsed.capture_format, parsed.records)
    if parsed.submitted and not usable:
        # The provider answered, but nothing in the answer can be used: EYE was
        # not observing. Only an empty, healthy response is a measured zero.
        return {
            "state": "failed",
            "value": None,
            "reason": f"all {parsed.submitted} record(s) rejected; nothing usable",
        }
    if parsed.capture_format == EVENT_CAPTURE_FORMAT:
        distinct = len({c.case_id for c in parsed.claims})  # cases, not deliveries
    elif parsed.capture_format == MEDIA_CAPTURE_FORMAT:
        distinct = len({i.item_id for i in parsed.items})  # items, not versions
    else:
        distinct = len({r.source_record_id for r in parsed.records})
    reasons = []
    if (parsed.observed_start, parsed.observed_end) != (
        parsed.requested_start,
        parsed.requested_end,
    ):
        reasons.append(
            f"provider covered {iso(parsed.observed_start)} to {iso(parsed.observed_end)} only"
        )
    if parsed.rejected:
        reasons.append(f"{len(parsed.rejected)} record(s) rejected")
    if reasons:
        return {"state": "partial", "value": distinct, "reason": "; ".join(reasons)}
    return {"state": "qualified", "value": distinct, "reason": None}


def observation_id(source_id: str, layer: str, record: Record) -> str:
    """Content-derived id. A source record is identified by (source, layer,
    record id): the same record id in two layers is two different records."""
    return stable_id(
        "observation",
        source_id,
        layer,
        record.source_record_id,
        iso(record.observed_time),
        record.content_sha256,
    )


def claim_id(source_id: str, layer: str, claim: event_claims.Claim) -> str:
    """Content-derived: a duplicate delivery of one case version is one claim."""
    return stable_id("event_claim", source_id, layer, claim.case_id, claim.content_sha256(iso))


def claim_row(source_id: str, layer: str, claim: event_claims.Claim) -> tuple:
    loc = claim.location
    coords = (loc.coords,) if loc.type == "point" else loc.coords
    window = claim.subject_window
    return (
        source_id,
        layer,
        claim.case_id,
        claim.basis,
        claim.kind,
        claim.status,
        None if claim.event_time is None else iso(claim.event_time),
        claim.event_time_uncertainty_s,
        iso(claim.source_published_time),
        loc.type,
        tuple(tuple(p) for p in coords),
        loc.precision_m,
        loc.direction,
        claim.evidence_ref,
        tuple(sorted(claim.subject_identifiers)),
        None if window is None else iso(window[0]),
        None if window is None else iso(window[1]),
        claim.summary,
        claim.content_sha256(iso),
    )


def media_item_id(source_id: str, item: media_items.Item) -> str:
    """Content-derived: a duplicate delivery of one item version is one media item."""
    return stable_id("media_item", source_id, item.item_id, item.content_sha256(iso))


def media_row(source_id: str, item: media_items.Item) -> tuple:
    place = item.place
    return (
        source_id,
        item.item_id,
        item.kind,
        item.status,
        iso(item.first_published_time),
        iso(item.revision_time),
        item.url,
        item.syndicated_from,
        item.publisher,
        item.creator,
        item.headline,
        item.language,
        item.rights,
        item.licence,
        item.attribution,
        None if item.capture_time_claimed is None else iso(item.capture_time_claimed),
        None if place is None else place.role,
        None if place is None else place.method,
        None if place is None else place.lon,
        None if place is None else place.lat,
        None if place is None else place.precision_m,
        item.content_sha256(iso),
    )


@dataclass(frozen=True)
class Derived:
    """Everything one committed batch contributes, as stored values."""

    batch: tuple
    observations: dict[str, tuple]
    receipts: dict[tuple[str, str], tuple]
    coverage: dict[str, tuple]
    claims: dict[str, tuple] = field(default_factory=dict)
    claim_receipts: dict[tuple[str, str], tuple] = field(default_factory=dict)
    media: dict[str, tuple] = field(default_factory=dict)
    media_receipts: dict[tuple[str, str], tuple] = field(default_factory=dict)


def derive(parsed: ParsedCapture) -> Derived:
    observations: dict[str, tuple] = {}
    receipts: dict[tuple[str, str], tuple] = {}
    for record in parsed.records:
        oid = observation_id(parsed.source_id, parsed.layer, record)
        observations[oid] = (
            parsed.source_id,
            record.source_record_id,
            parsed.layer,
            "observed",
            iso(record.observed_time),
            iso(record.source_published_time),
            record.lon,
            record.lat,
            record.alt_m,
            record.confidence,
            tuple(record.quality_flags),
            record.content_sha256,
        )
        receipts[(oid, parsed.batch_id)] = (
            parsed.evidence_id,
            iso(parsed.received_time),
            RECEIPT_SCHEMA_VERSION,
            parsed.adapter_version,
        )
    claims: dict[str, tuple] = {}
    claim_receipts: dict[tuple[str, str], tuple] = {}
    for claim in parsed.claims:
        cid = claim_id(parsed.source_id, parsed.layer, claim)
        claims[cid] = claim_row(parsed.source_id, parsed.layer, claim)
        claim_receipts[(cid, parsed.batch_id)] = (
            parsed.evidence_id,
            iso(parsed.received_time),
            parsed.adapter_version,
        )
    media: dict[str, tuple] = {}
    media_receipts: dict[tuple[str, str], tuple] = {}
    for item in parsed.items:
        mid = media_item_id(parsed.source_id, item)
        media[mid] = media_row(parsed.source_id, item)
        media_receipts[(mid, parsed.batch_id)] = (
            parsed.evidence_id,
            iso(parsed.received_time),
            parsed.adapter_version,
        )
    coverage = derive_coverage(parsed)
    metric = parsed.metric_name
    coverage_id = stable_id("coverage", parsed.batch_id, parsed.layer, metric)
    accepted = len(parsed.records) + len(parsed.claims) + len(parsed.items)
    return Derived(
        batch=("committed", accepted, len(parsed.rejected), None),
        observations=observations,
        receipts=receipts,
        claims=claims,
        claim_receipts=claim_receipts,
        media=media,
        media_receipts=media_receipts,
        coverage={
            coverage_id: (
                parsed.batch_id,
                parsed.source_id,
                parsed.layer,
                iso(parsed.requested_start),
                iso(parsed.requested_end),
                coverage["state"],
                coverage["reason"],
                metric,
                coverage["value"],
                DERIVATION_VERSION,
            )
        },
    )


INTEGRITY_REASON = "evidence checksum mismatch"


def derive_integrity_failure(batch_id: str, facts: tuple) -> Derived:
    """What a batch whose stored evidence fails its checksum must contain."""
    source_id, layer, start, end, capture_format = facts
    metric = metric_for(capture_format)
    return Derived(
        batch=("failed", 0, 0, INTEGRITY_REASON),
        observations={},
        receipts={},
        coverage={
            stable_id("coverage", batch_id, "integrity", metric): (
                batch_id,
                source_id,
                layer,
                start,
                end,
                "failed",
                INTEGRITY_REASON,
                metric,
                None,
                DERIVATION_VERSION,
            )
        },
    )


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    status: str
    accepted: int | None
    rejected: int | None
    created: bool


def archive(conn: Connection, raw: bytes) -> ParsedCapture:
    """Store the raw bytes and a pending batch. Idempotent."""
    parsed = parse_capture(raw)
    west, south, east, north = parsed.bbox
    with transaction(conn):
        conn.run(
            """
            INSERT INTO eye.capture_batch (
                batch_id, source_id, layer, adapter_version, capture_format, requested_area,
                requested_start, requested_end, expected_interval_s, attempt_started_at,
                attempt_finished_at, provider_status, quota_cost, evidence_sha256)
            VALUES (:batch_id, :source_id, :layer, :adapter_version, :capture_format,
                ST_MakeEnvelope(:west, :south, :east, :north, 4326),
                CAST(:rstart AS timestamptz), CAST(:rend AS timestamptz), :expected,
                CAST(:started AS timestamptz), CAST(:finished AS timestamptz), :status, :quota,
                :sha)
            ON CONFLICT (batch_id) DO NOTHING
            """,
            batch_id=parsed.batch_id,
            source_id=parsed.source_id,
            layer=parsed.layer,
            adapter_version=parsed.adapter_version,
            capture_format=parsed.capture_format,
            west=west,
            south=south,
            east=east,
            north=north,
            rstart=iso(parsed.requested_start),
            rend=iso(parsed.requested_end),
            expected=parsed.expected_interval_s,
            started=iso(parsed.started_at),
            finished=iso(parsed.finished_at),
            status=parsed.provider_status,
            quota=parsed.quota_cost,
            sha=parsed.sha256,
        )
        conn.run(
            """
            INSERT INTO eye.raw_evidence
                (evidence_id, batch_id, sha256, byte_size, media_type, content)
            VALUES (:evidence_id, :batch_id, :sha, :size, 'application/json', :content)
            ON CONFLICT (evidence_id) DO NOTHING
            """,
            evidence_id=parsed.evidence_id,
            batch_id=parsed.batch_id,
            sha=parsed.sha256,
            size=parsed.size,
            content=raw,
        )
    return parsed


def _write(conn: Connection, batch_id: str, derived: Derived) -> None:
    for oid, row in derived.observations.items():
        (source, record, layer, display, observed, published, lon, lat, alt, conf, flags, sha) = row
        conn.run(
            """
            INSERT INTO eye.observation (
                observation_id, source_id, source_record_id, layer, display_type, observed_time,
                source_published_time, position, altitude_m, confidence, quality_flags,
                content_sha256)
            VALUES (:id, :source, :record, :layer, CAST(:display AS eye.display_type),
                CAST(:observed AS timestamptz), CAST(:published AS timestamptz),
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :alt, :confidence,
                CAST(:flags AS text[]), :sha)
            ON CONFLICT (observation_id) DO NOTHING
            """,
            id=oid,
            source=source,
            record=record,
            layer=layer,
            display=display,
            observed=observed,
            published=published,
            lon=lon,
            lat=lat,
            alt=alt,
            confidence=conf,
            flags=list(flags),
            sha=sha,
        )
    for (oid, bid), (
        evidence_id,
        received,
        schema_version,
        adapter_version,
    ) in derived.receipts.items():
        conn.run(
            """
            INSERT INTO eye.observation_receipt (
                observation_id, batch_id, evidence_id, received_time, schema_version,
                adapter_version)
            VALUES (:oid, :bid, :eid, CAST(:received AS timestamptz), :schema, :adapter)
            ON CONFLICT (observation_id, batch_id) DO NOTHING
            """,
            oid=oid,
            bid=bid,
            eid=evidence_id,
            received=received,
            schema=schema_version,
            adapter=adapter_version,
        )
    for cid, row in derived.coverage.items():
        (bid, source, layer, start, end, state, reason, metric, value, derivation) = row
        conn.run(
            """
            INSERT INTO eye.coverage (
                coverage_id, batch_id, source_id, layer, interval_start, interval_end, state,
                reason, metric_name, metric_value, derivation_version)
            VALUES (:id, :bid, :source, :layer, CAST(:start AS timestamptz),
                CAST(:end AS timestamptz), CAST(:state AS eye.coverage_state), :reason, :metric,
                :value, :derivation)
            ON CONFLICT (coverage_id) DO NOTHING
            """,
            id=cid,
            bid=bid,
            source=source,
            layer=layer,
            start=start,
            end=end,
            state=state,
            reason=reason,
            metric=metric,
            value=value,
            derivation=derivation,
        )
    for cid, row in derived.claims.items():
        (
            source,
            layer,
            case,
            basis,
            kind,
            status,
            event_time,
            uncertainty,
            published,
            loc_type,
            coords,
            precision,
            direction,
            evidence,
            identifiers,
            w_start,
            w_end,
            summary,
            sha,
        ) = row
        conn.run(
            """
            INSERT INTO eye.event_claim (
                claim_id, source_id, layer, case_id, basis, kind, status, event_time,
                event_time_uncertainty_s, source_published_time, location_type, location,
                precision_m, segment_direction, evidence_ref, subject_identifiers,
                subject_window_start, subject_window_end, summary, content_sha256)
            VALUES (:id, :source, :layer, :case, CAST(:basis AS eye.claim_basis), :kind,
                CAST(:status AS eye.claim_status), CAST(:event_time AS timestamptz), :uncertainty,
                CAST(:published AS timestamptz), :loc_type, ST_GeomFromText(:wkt, 4326),
                :precision, :direction, :evidence, CAST(:identifiers AS text[]),
                CAST(:w_start AS timestamptz), CAST(:w_end AS timestamptz), :summary, :sha)
            ON CONFLICT (claim_id) DO NOTHING
            """,
            id=cid,
            source=source,
            layer=layer,
            case=case,
            basis=basis,
            kind=kind,
            status=status,
            event_time=event_time,
            uncertainty=uncertainty,
            published=published,
            loc_type=loc_type,
            wkt=_wkt(loc_type, coords),
            precision=precision,
            direction=direction,
            evidence=evidence,
            identifiers=list(identifiers),
            w_start=w_start,
            w_end=w_end,
            summary=summary,
            sha=sha,
        )
    for (cid, bid), (evidence_id, received, adapter_version) in derived.claim_receipts.items():
        conn.run(
            """
            INSERT INTO eye.event_claim_receipt (
                claim_id, batch_id, evidence_id, received_time, adapter_version)
            VALUES (:cid, :bid, :eid, CAST(:received AS timestamptz), :adapter)
            ON CONFLICT (claim_id, batch_id) DO NOTHING
            """,
            cid=cid,
            bid=bid,
            eid=evidence_id,
            received=received,
            adapter=adapter_version,
        )
    for mid, row in derived.media.items():
        (
            source,
            item,
            kind,
            status,
            first,
            revision,
            link,
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
            sha,
        ) = row
        conn.run(
            """
            INSERT INTO eye.media_item (
                media_item_id, source_id, item_id, kind, status, first_published_time,
                revision_time, url, syndicated_from, publisher, creator, headline, language,
                rights_status, licence, attribution, capture_time_claimed, place_role,
                place_method, place, place_precision_m, content_sha256)
            VALUES (:id, :source, :item, :kind, :status, CAST(:first AS timestamptz),
                CAST(:revision AS timestamptz), :url, :syndicated, :publisher, :creator,
                :headline, :language, :rights, :licence, :attribution,
                CAST(:captured AS timestamptz), :role, :method,
                CASE WHEN CAST(:lon AS double precision) IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) END,
                :precision, :sha)
            ON CONFLICT (media_item_id) DO NOTHING
            """,
            id=mid,
            source=source,
            item=item,
            kind=kind,
            status=status,
            first=first,
            revision=revision,
            url=link,
            syndicated=syndicated,
            publisher=publisher,
            creator=creator,
            headline=headline,
            language=language,
            rights=rights,
            licence=licence,
            attribution=attribution,
            captured=captured,
            role=role,
            method=method,
            lon=lon,
            lat=lat,
            precision=precision,
            sha=sha,
        )
    for (mid, bid), (evidence_id, received, adapter_version) in derived.media_receipts.items():
        conn.run(
            """
            INSERT INTO eye.media_item_receipt (
                media_item_id, batch_id, evidence_id, received_time, adapter_version)
            VALUES (:mid, :bid, :eid, CAST(:received AS timestamptz), :adapter)
            ON CONFLICT (media_item_id, batch_id) DO NOTHING
            """,
            mid=mid,
            bid=bid,
            eid=evidence_id,
            received=received,
            adapter=adapter_version,
        )
    status, accepted, rejected, reason = derived.batch
    conn.run(
        "UPDATE eye.capture_batch SET status = CAST(:status AS eye.batch_status), "
        "accepted_count = :a, rejected_count = :r, failure_reason = :reason, "
        "committed_at = now() WHERE batch_id = :id",
        status=status,
        a=accepted,
        r=rejected,
        reason=reason,
        id=batch_id,
    )


BATCH_FACTS = """
    SELECT b.status::text, b.accepted_count, b.rejected_count, b.failure_reason,
           b.evidence_sha256, e.content, b.source_id, b.layer,
           eye.iso_utc(b.requested_start), eye.iso_utc(b.requested_end), b.capture_format
    FROM eye.capture_batch b JOIN eye.raw_evidence e USING (batch_id)
"""


def _wkt(loc_type: str, coords: tuple) -> str:
    return event_claims.Location(
        loc_type, coords[0] if loc_type == "point" else coords, 0.0, None
    ).wkt()


def _expected_for(batch_id: str, row: list) -> Derived:
    """Derive what a batch must contain from its stored evidence alone."""
    content, expected_sha, facts = bytes(row[5]), row[4], tuple(row[6:11])
    if hashlib.sha256(content).hexdigest() != expected_sha:
        return derive_integrity_failure(batch_id, facts)
    return derive(parse_capture(content))


def commit_batch(conn: Connection, batch_id: str) -> BatchResult:
    """Derive a batch from its stored evidence and commit the result. Idempotent."""
    with transaction(conn):
        rows = conn.run(BATCH_FACTS + " WHERE b.batch_id = :id FOR UPDATE OF b", id=batch_id)
        if not rows:
            raise LookupError(f"no archived batch {batch_id}")
        status, accepted, rejected = rows[0][0], rows[0][1], rows[0][2]
        if status != "pending":
            return BatchResult(batch_id, status, accepted, rejected, created=False)
        derived = _expected_for(batch_id, rows[0])
        _write(conn, batch_id, derived)
    status, accepted, rejected, _ = derived.batch
    return BatchResult(batch_id, status, accepted, rejected, created=True)


def ingest(conn: Connection, raw: bytes) -> BatchResult:
    parsed = archive(conn, raw)
    return commit_batch(conn, parsed.batch_id)


def replay_pending(conn: Connection) -> list[BatchResult]:
    """Complete every batch left pending by an interrupted capture, oldest first."""
    pending = conn.run(
        "SELECT batch_id FROM eye.capture_batch WHERE status = 'pending' "
        "ORDER BY archived_at, batch_id"
    )
    return [commit_batch(conn, str(batch_id)) for (batch_id,) in pending]


def load_fixtures(conn: Connection, directory: Path, *, reverse: bool = False) -> list[BatchResult]:
    """Ingest every capture fixture in name order (or reversed, for tests). Safe to repeat."""
    paths = sorted(directory.glob("*.json"), reverse=reverse)
    if not paths:
        raise FileNotFoundError(f"no capture fixtures in {directory}")
    return [ingest(conn, path.read_bytes()) for path in paths]


# --- verification -------------------------------------------------------------


def _normalise(value: object) -> object:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _rows(conn: Connection, sql: str) -> list[tuple]:
    return [tuple(_normalise(v) for v in row) for row in conn.run(sql)]


def expected_versions(observations: dict[str, tuple]) -> dict[str, tuple]:
    """Version facts per source record and observed time (mirrors eye.observation_version).

    A source record is (source, layer, record id); versions are compared only
    within one record, never across layers.

    Versions are ranked by source publication time. Versions sharing a
    publication time are a conflict: same version number, no supersedes link
    in either direction, and when they are the latest, current is unknown (None).
    """
    groups: dict[tuple, dict[str, list[str]]] = {}
    for oid, row in observations.items():
        source, record, layer, observed, published = row[0], row[1], row[2], row[4], row[5]
        tiers = groups.setdefault((source, layer, record, observed), {})
        tiers.setdefault(published, []).append(oid)
    return _version_facts(groups)


def expected_claim_versions(claims: dict[str, tuple]) -> dict[str, tuple]:
    """Version facts per case (mirrors eye.event_claim_version): same rules."""
    groups: dict[tuple, dict[str, list[str]]] = {}
    for cid, row in claims.items():
        source, layer, case, published = row[0], row[1], row[2], row[8]
        groups.setdefault((source, layer, case), {}).setdefault(published, []).append(cid)
    return _version_facts(groups)


def _version_facts(groups: dict[tuple, dict[str, list[str]]]) -> dict[str, tuple]:
    result: dict[str, tuple] = {}
    for tiers in groups.values():
        ordered = sorted(tiers)
        latest = ordered[-1]
        for rank, published in enumerate(ordered, start=1):
            members = tiers[published]
            unique = len(members) == 1
            previous = tiers[ordered[rank - 2]] if rank > 1 else []
            supersedes = previous[0] if unique and len(previous) == 1 else None
            # Latest and contested: unknown (None), never an arbitrary pick.
            current = (unique or None) if published == latest else False
            for oid in members:
                result[oid] = (rank, supersedes, current, not unique)
    return result


def _compare(label: str, expected: dict, actual: dict, problems: list[str]) -> None:
    for key in sorted(set(expected) | set(actual), key=str):
        if key not in actual:
            problems.append(f"{label} {key}: missing from the database")
        elif key not in expected:
            problems.append(f"{label} {key}: stored but not derivable from any evidence")
        elif expected[key] != actual[key]:
            problems.append(f"{label} {key}: stored {actual[key]!r}, derived {expected[key]!r}")


ARCHIVE_FACTS = """
    SELECT b.batch_id::text, b.source_id, b.layer, b.adapter_version, b.capture_format,
           ST_XMin(b.requested_area), ST_YMin(b.requested_area), ST_XMax(b.requested_area),
           ST_YMax(b.requested_area), ST_Equals(b.requested_area, ST_Envelope(b.requested_area)),
           eye.iso_utc(b.requested_start), eye.iso_utc(b.requested_end), b.expected_interval_s,
           eye.iso_utc(b.attempt_started_at), eye.iso_utc(b.attempt_finished_at),
           b.provider_status, b.quota_cost, b.evidence_sha256,
           e.evidence_id::text, e.sha256, e.byte_size, e.media_type, e.content
    FROM eye.capture_batch b LEFT JOIN eye.raw_evidence e USING (batch_id)
    ORDER BY b.batch_id
"""


def _verify_archive(conn: Connection, problems: list[str]) -> None:
    """Compare each batch's stored capture facts and evidence metadata with its bytes."""
    for row in conn.run(ARCHIVE_FACTS):
        batch_id, facts, evidence, content = row[0], row[1:18], row[18:22], row[22]
        if content is None:
            problems.append(f"capture {batch_id}: archived batch has no raw evidence")
            continue
        content = bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        stored_evidence = tuple(_normalise(v) for v in evidence)
        expected_evidence = (stable_id("evidence", sha), sha, len(content), "application/json")
        if stored_evidence != expected_evidence:
            problems.append(
                f"evidence {batch_id}: stored metadata {stored_evidence!r}, "
                f"archived bytes give {expected_evidence!r}"
            )
        try:
            parsed = parse_capture(content)
        except CaptureRejected as exc:
            problems.append(f"capture {batch_id}: archived bytes are unreadable ({exc})")
            continue
        west, south, east, north = parsed.bbox
        expected_facts = (
            parsed.source_id,
            parsed.layer,
            parsed.adapter_version,
            parsed.capture_format,
            west,
            south,
            east,
            north,
            True,
            iso(parsed.requested_start),
            iso(parsed.requested_end),
            parsed.expected_interval_s,
            iso(parsed.started_at),
            iso(parsed.finished_at),
            parsed.provider_status,
            parsed.quota_cost,
            sha,
        )
        stored_facts = tuple(_normalise(v) for v in facts)
        if stored_facts != expected_facts:
            fields = [
                name
                for name, got, want in zip(
                    CAPTURE_FACT_NAMES, stored_facts, expected_facts, strict=True
                )
                if got != want
            ]
            problems.append(
                f"capture {batch_id}: stored facts differ from archived bytes: {', '.join(fields)}"
            )


CAPTURE_FACT_NAMES = (
    "source_id",
    "layer",
    "adapter_version",
    "capture_format",
    "requested_area.west",
    "requested_area.south",
    "requested_area.east",
    "requested_area.north",
    "requested_area.is_rectangle",
    "requested_start",
    "requested_end",
    "expected_interval_s",
    "attempt_started_at",
    "attempt_finished_at",
    "provider_status",
    "quota_cost",
    "evidence_sha256",
)


def verify_replay(conn: Connection) -> list[str]:
    """Re-derive every row from stored evidence and compare every value and link.

    Returns discrepancies; an empty list means the stored capture facts and
    evidence metadata match the archived bytes, and replaying the evidence would
    reproduce the database exactly (ids, values, receipts, coverage, batch
    outcomes and version links). It cannot detect bytes and every checksum
    rewritten consistently; that needs the independent backup (Package 5).
    """
    problems: list[str] = []
    expected_batches: dict[str, tuple] = {}
    observations: dict[str, tuple] = {}
    receipts: dict[tuple, tuple] = {}
    coverage: dict[str, tuple] = {}
    claims: dict[str, tuple] = {}
    claim_receipts: dict[tuple, tuple] = {}
    media: dict[str, tuple] = {}
    media_receipts: dict[tuple, tuple] = {}
    for batch_id, *row in conn.run(
        BATCH_FACTS.replace("SELECT ", "SELECT b.batch_id::text, ", 1) + " ORDER BY b.batch_id"
    ):
        if row[0] == "pending":
            problems.append(f"batch {batch_id}: still pending; run db-replay")
            continue
        content, sha = bytes(row[5]), row[4]
        if hashlib.sha256(content).hexdigest() != sha:
            problems.append(f"batch {batch_id}: stored evidence does not match its checksum")
        derived = _expected_for(batch_id, row)
        try:
            parsed_id = parse_capture(content).batch_id
        except CaptureRejected:
            parsed_id = batch_id  # unreadable evidence is reported above
        if parsed_id != batch_id:
            problems.append(f"batch {batch_id}: evidence derives batch id {parsed_id}")
        expected_batches[batch_id] = derived.batch
        observations.update(derived.observations)
        receipts.update(derived.receipts)
        coverage.update(derived.coverage)
        claims.update(derived.claims)
        claim_receipts.update(derived.claim_receipts)
        media.update(derived.media)
        media_receipts.update(derived.media_receipts)

    _verify_archive(conn, problems)

    actual_batches = {
        r[0]: r[1:]
        for r in _rows(
            conn,
            "SELECT batch_id::text, status::text, accepted_count, rejected_count, failure_reason "
            "FROM eye.capture_batch WHERE status <> 'pending'",
        )
    }
    _compare("batch", expected_batches, actual_batches, problems)

    actual_observations = {
        r[0]: r[1:]
        for r in _rows(
            conn,
            "SELECT observation_id::text, source_id, source_record_id, layer, "
            "display_type::text, eye.iso_utc(observed_time), "
            "eye.iso_utc(source_published_time), ST_X(position), ST_Y(position), altitude_m, "
            "confidence, quality_flags, content_sha256 FROM eye.observation",
        )
    }
    _compare("observation", observations, actual_observations, problems)

    actual_receipts = {
        (r[0], r[1]): r[2:]
        for r in _rows(
            conn,
            "SELECT observation_id::text, batch_id::text, evidence_id::text, "
            "eye.iso_utc(received_time), schema_version, adapter_version "
            "FROM eye.observation_receipt",
        )
    }
    _compare("receipt", receipts, actual_receipts, problems)

    actual_coverage = {
        r[0]: r[1:]
        for r in _rows(
            conn,
            "SELECT coverage_id::text, batch_id::text, source_id, layer, "
            "eye.iso_utc(interval_start), eye.iso_utc(interval_end), state::text, reason, "
            "metric_name, metric_value, derivation_version FROM eye.coverage",
        )
    }
    _compare("coverage", coverage, actual_coverage, problems)

    actual_versions = {
        r[0]: r[1:]
        for r in _rows(
            conn,
            "SELECT observation_id::text, version, supersedes_observation_id::text, is_current, "
            "publication_conflict FROM eye.observation_version",
        )
    }
    _compare("version", expected_versions(observations), actual_versions, problems)

    actual_claims = {}
    for row in conn.run(CLAIM_FACTS):
        values = [_normalise(v) for v in row]
        values[11] = tuple(tuple(p) for p in row[11])  # coordinates, in order
        actual_claims[values[0]] = tuple(values[1:])
    _compare("event claim", claims, actual_claims, problems)
    actual_claim_receipts = {
        (r[0], r[1]): r[2:]
        for r in _rows(
            conn,
            "SELECT claim_id::text, batch_id::text, evidence_id::text, "
            "eye.iso_utc(received_time), adapter_version FROM eye.event_claim_receipt",
        )
    }
    _compare("event claim receipt", claim_receipts, actual_claim_receipts, problems)
    actual_claim_versions = {
        r[0]: r[1:]
        for r in _rows(
            conn,
            "SELECT claim_id::text, version, supersedes_claim_id::text, is_current, "
            "publication_conflict FROM eye.event_claim_version",
        )
    }
    _compare(
        "event claim version", expected_claim_versions(claims), actual_claim_versions, problems
    )
    actual_media = {r[0]: r[1:] for r in _rows(conn, MEDIA_FACTS)}
    _compare("media item", media, actual_media, problems)
    actual_media_receipts = {
        (r[0], r[1]): r[2:]
        for r in _rows(
            conn,
            "SELECT media_item_id::text, batch_id::text, evidence_id::text, "
            "eye.iso_utc(received_time), adapter_version FROM eye.media_item_receipt",
        )
    }
    _compare("media item receipt", media_receipts, actual_media_receipts, problems)
    actual_media_versions = {
        r[0]: r[1:]
        for r in _rows(
            conn,
            "SELECT media_item_id::text, version, supersedes_media_item_id::text, is_current, "
            "revision_conflict FROM eye.media_item_version",
        )
    }
    _compare("media item version", expected_media_versions(media), actual_media_versions, problems)
    return problems


def expected_media_versions(media: dict[str, tuple]) -> dict[str, tuple]:
    """Version facts per item (mirrors eye.media_item_version): same rules."""
    groups: dict[tuple, dict[str, list[str]]] = {}
    for mid, row in media.items():
        source, item, revision = row[0], row[1], row[5]
        groups.setdefault((source, item), {}).setdefault(revision, []).append(mid)
    return _version_facts(groups)


MEDIA_FACTS = """
    SELECT media_item_id::text, source_id, item_id, kind, status,
           eye.iso_utc(first_published_time), eye.iso_utc(revision_time), url, syndicated_from,
           publisher, creator, headline, language, rights_status, licence, attribution,
           eye.iso_utc(capture_time_claimed), place_role, place_method, ST_X(place),
           ST_Y(place), place_precision_m, content_sha256
    FROM eye.media_item
"""


CLAIM_FACTS = """
    SELECT claim_id::text, source_id, layer, case_id, basis::text, kind, status::text,
           eye.iso_utc(event_time), event_time_uncertainty_s, eye.iso_utc(source_published_time),
           location_type,
           (SELECT array_agg(ARRAY[ST_X(d.geom), ST_Y(d.geom)] ORDER BY d.path)
            FROM ST_DumpPoints(location) d),
           precision_m, segment_direction, evidence_ref,
           ARRAY(SELECT i FROM unnest(subject_identifiers) i ORDER BY i),
           eye.iso_utc(subject_window_start), eye.iso_utc(subject_window_end), summary,
           content_sha256
    FROM eye.event_claim
"""
