"""Capture pipeline: archive raw evidence, then commit parsed records.

1. ``archive`` stores the exact bytes and a ``pending`` batch in one transaction.
2. ``commit_batch`` re-reads those stored bytes (never the caller's copy),
   checks the checksum, parses them and writes observations and coverage in a
   second transaction, then marks the batch ``committed``.

A crash between the steps leaves a discoverable pending batch that
``replay_pending`` completes. Every id is derived from content, so repeating
any step, reloading fixtures or rebuilding the database reproduces the same ids.
Only sources with an approved row in docs/source-policy-register.md are accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eye.storage.db import Connection, transaction
from eye.wire import SCHEMA_VERSION

CAPTURE_FORMAT = "eye.synthetic-capture/1"
APPROVED_SOURCES = frozenset({"synthetic-fixture"})
LAYERS = frozenset({"flight", "vessel", "road"})
PROVIDER_STATUSES = frozenset({"ok", "error", "timeout", "indeterminate"})
MAX_EVIDENCE_BYTES = 1_048_576
MAX_RECORDS = 10_000
DERIVATION_VERSION = "coverage/1"
METRIC_NAME = "tracks_observed"
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://eye.invalid/ids/v1")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TIMESTAMP = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(\.[0-9]{1,6})?Z\Z"
)


class CaptureRejected(ValueError):
    """The capture envelope is unusable; nothing was archived."""


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, "|".join(parts)))


def parse_time(value: object, what: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP.match(value):
        raise ValueError(f"{what} must be an RFC 3339 UTC timestamp ending in Z")
    return datetime.fromisoformat(value).astimezone(UTC)


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
    received_time: datetime
    lon: float
    lat: float
    alt_m: float | None
    confidence: float | None
    quality_flags: tuple[str, ...]

    @property
    def content_sha256(self) -> str:
        # Receipt time is deliberately excluded: a duplicate delivery of the same
        # observation is the same observation, not independent confirmation.
        content = {
            "alt_m": self.alt_m,
            "confidence": self.confidence,
            "lat": self.lat,
            "lon": self.lon,
            "quality_flags": sorted(self.quality_flags),
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
    records: list[Record] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def batch_id(self) -> str:
        return stable_id("batch", self.source_id, self.sha256)

    @property
    def evidence_id(self) -> str:
        return stable_id("evidence", self.sha256)


def _parse_record(raw: object, index: int) -> Record:
    if not isinstance(raw, dict):
        raise ValueError(f"record {index} is not an object")
    allowed = {
        "record_id",
        "observed_time",
        "received_time",
        "lon",
        "lat",
        "alt_m",
        "confidence",
        "quality_flags",
    }
    if set(raw) - allowed:
        raise ValueError(f"record {index} has unexpected fields {sorted(set(raw) - allowed)}")
    record_id = raw.get("record_id")
    if not isinstance(record_id, str) or not IDENTIFIER.match(record_id):
        raise ValueError(f"record {index} has an invalid record_id")
    flags = raw.get("quality_flags", [])
    if not isinstance(flags, list) or len(flags) > 16:
        raise ValueError(f"record {index} quality_flags must be a list of at most 16")
    if not all(isinstance(f, str) and IDENTIFIER.match(f) for f in flags):
        raise ValueError(f"record {index} has an invalid quality flag")
    observed = parse_time(raw.get("observed_time"), f"record {index} observed_time")
    received = parse_time(raw.get("received_time"), f"record {index} received_time")
    if received < observed:
        flags = [*flags, "received_before_observed"]
    alt = raw.get("alt_m")
    confidence = raw.get("confidence")
    return Record(
        source_record_id=record_id,
        observed_time=observed,
        received_time=received,
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
        if doc.get("capture_format") != CAPTURE_FORMAT:
            raise ValueError(f"capture_format must be {CAPTURE_FORMAT}")
        if doc.get("synthetic") is not True:
            raise ValueError('only captures marked "synthetic": true are accepted')
        source_id = doc.get("source_id")
        if source_id not in APPROVED_SOURCES:
            raise ValueError(
                f"source {source_id!r} has no approved row in docs/source-policy-register.md"
            )
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
        attempt = doc["attempt"]
        status = attempt["provider_status"]
        if status not in PROVIDER_STATUSES:
            raise ValueError(f"attempt.provider_status must be one of {sorted(PROVIDER_STATUSES)}")
        started = parse_time(attempt["started_at"], "attempt.started_at")
        finished = parse_time(attempt["finished_at"], "attempt.finished_at")
        if finished < started:
            raise ValueError("attempt.finished_at is before attempt.started_at")
        quota = _number(attempt["quota_cost"], "attempt.quota_cost", 0, 1e9)
        observed_start = observed_end = None
        if status == "ok":
            observed_start = parse_time(attempt["observed_start"], "attempt.observed_start")
            observed_end = parse_time(attempt["observed_end"], "attempt.observed_end")
            if not start <= observed_start <= observed_end <= end:
                raise ValueError("observed interval must lie inside the requested interval")
        records = doc["records"]
        if not isinstance(records, list) or len(records) > MAX_RECORDS:
            raise ValueError(f"records must be a list of at most {MAX_RECORDS}")
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
    )
    for index, item in enumerate(records):
        if status != "ok":
            parsed.rejected.append(f"record {index}: provider status {status}")
            continue
        try:
            record = _parse_record(item, index)
        except ValueError as exc:
            parsed.rejected.append(str(exc))
            continue
        if not start <= record.observed_time <= end:
            parsed.rejected.append(f"record {index}: observed_time outside the request")
            continue
        parsed.records.append(record)
    return parsed


def derive_coverage(parsed: ParsedCapture, accepted_records: int) -> dict:
    """Coverage for one batch. An outage is failed/unknown with no value, never zero."""
    status = parsed.provider_status
    if status in ("error", "timeout"):
        return {"state": "failed", "value": None, "reason": f"provider status {status}"}
    if status == "indeterminate":
        return {"state": "unknown", "value": None, "reason": "provider completeness unknown"}
    distinct = len({r.source_record_id for r in parsed.records}) if accepted_records else 0
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
            capture_format=CAPTURE_FORMAT,
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


def _insert_observation(conn: Connection, parsed: ParsedCapture, record: Record) -> bool:
    observed = iso(record.observed_time)
    observation_id = stable_id(
        "observation", parsed.source_id, record.source_record_id, observed, record.content_sha256
    )
    previous = conn.run(
        """
        SELECT observation_id FROM eye.observation
        WHERE source_id = :source AND source_record_id = :record
          AND observed_time = CAST(:observed AS timestamptz) AND observation_id <> :id
        ORDER BY received_time DESC, observation_id DESC LIMIT 1
        """,
        source=parsed.source_id,
        record=record.source_record_id,
        observed=observed,
        id=observation_id,
    )
    rows = conn.run(
        """
        INSERT INTO eye.observation (
            observation_id, source_id, source_record_id, layer, observed_time, received_time,
            position, altitude_m, confidence, quality_flags, content_sha256,
            supersedes_observation_id, batch_id, evidence_id, schema_version, adapter_version)
        VALUES (:id, :source, :record, :layer, CAST(:observed AS timestamptz),
            CAST(:received AS timestamptz), ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), :alt,
            :confidence, CAST(:flags AS text[]), :content, CAST(:supersedes AS uuid), :batch_id,
            :evidence_id, :schema_version, :adapter_version)
        ON CONFLICT (observation_id) DO NOTHING
        RETURNING observation_id
        """,
        id=observation_id,
        source=parsed.source_id,
        record=record.source_record_id,
        layer=parsed.layer,
        observed=observed,
        received=iso(record.received_time),
        lon=record.lon,
        lat=record.lat,
        alt=record.alt_m,
        confidence=record.confidence,
        flags=list(record.quality_flags),
        content=record.content_sha256,
        supersedes=previous[0][0] if previous else None,
        batch_id=parsed.batch_id,
        evidence_id=parsed.evidence_id,
        schema_version=SCHEMA_VERSION,
        adapter_version=parsed.adapter_version,
    )
    return bool(rows)


def commit_batch(conn: Connection, batch_id: str) -> BatchResult:
    """Parse a batch's stored evidence and commit its records. Idempotent."""
    with transaction(conn):
        rows = conn.run(
            """
            SELECT b.status, b.accepted_count, b.rejected_count, b.evidence_sha256, e.content
            FROM eye.capture_batch b JOIN eye.raw_evidence e USING (batch_id)
            WHERE b.batch_id = :id FOR UPDATE OF b
            """,
            id=batch_id,
        )
        if not rows:
            raise LookupError(f"no archived batch {batch_id}")
        status, accepted, rejected, expected_sha, content = rows[0]
        if status != "pending":
            return BatchResult(batch_id, status, accepted, rejected, created=False)
        content = bytes(content)
        if hashlib.sha256(content).hexdigest() != expected_sha:
            # Nothing from this batch can be trusted: record the gap as failed
            # coverage (never zero) using the capture facts, not the evidence.
            conn.run(
                """
                INSERT INTO eye.coverage (
                    coverage_id, batch_id, source_id, layer, interval_start, interval_end,
                    state, reason, metric_name, metric_value, derivation_version)
                SELECT CAST(:cid AS uuid), batch_id, source_id, layer, requested_start,
                    requested_end, 'failed', 'evidence checksum mismatch', :metric, NULL,
                    :derivation
                FROM eye.capture_batch WHERE batch_id = CAST(:id AS uuid)
                ON CONFLICT (coverage_id) DO NOTHING
                """,
                cid=stable_id("coverage", batch_id, "integrity", METRIC_NAME),
                id=batch_id,
                metric=METRIC_NAME,
                derivation=DERIVATION_VERSION,
            )
            conn.run(
                "UPDATE eye.capture_batch SET status = 'failed', accepted_count = 0, "
                "rejected_count = 0, committed_at = now(), "
                "failure_reason = 'evidence checksum mismatch' WHERE batch_id = :id",
                id=batch_id,
            )
            return BatchResult(batch_id, "failed", 0, 0, created=True)
        parsed = parse_capture(content)
        for record in parsed.records:
            _insert_observation(conn, parsed, record)
        accepted = len(parsed.records)
        coverage = derive_coverage(parsed, accepted)
        conn.run(
            """
            INSERT INTO eye.coverage (
                coverage_id, batch_id, source_id, layer, interval_start, interval_end, state,
                reason, metric_name, metric_value, derivation_version)
            VALUES (:id, :batch_id, :source, :layer, CAST(:start AS timestamptz),
                CAST(:end AS timestamptz), CAST(:state AS eye.coverage_state), :reason, :metric,
                :value, :derivation)
            ON CONFLICT (coverage_id) DO NOTHING
            """,
            id=stable_id("coverage", parsed.batch_id, parsed.layer, METRIC_NAME),
            batch_id=parsed.batch_id,
            source=parsed.source_id,
            layer=parsed.layer,
            start=iso(parsed.requested_start),
            end=iso(parsed.requested_end),
            state=coverage["state"],
            reason=coverage["reason"],
            metric=METRIC_NAME,
            value=coverage["value"],
            derivation=DERIVATION_VERSION,
        )
        conn.run(
            "UPDATE eye.capture_batch SET status = 'committed', accepted_count = :a, "
            "rejected_count = :r, committed_at = now() WHERE batch_id = :id",
            a=accepted,
            r=len(parsed.rejected),
            id=batch_id,
        )
    return BatchResult(batch_id, "committed", accepted, len(parsed.rejected), created=True)


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


def load_fixtures(conn: Connection, directory: Path) -> list[BatchResult]:
    """Ingest every capture fixture in name order. Safe to repeat."""
    results = [ingest(conn, path.read_bytes()) for path in sorted(directory.glob("*.json"))]
    if not results:
        raise FileNotFoundError(f"no capture fixtures in {directory}")
    return results


def verify_replay(conn: Connection) -> list[str]:
    """Re-derive ids from stored evidence and compare with stored rows.

    Returns a list of discrepancies; empty means replay reproduces the database.
    """
    problems: list[str] = []
    batches = conn.run(
        "SELECT b.batch_id, e.content FROM eye.capture_batch b JOIN eye.raw_evidence e "
        "USING (batch_id) WHERE b.status = 'committed' ORDER BY b.batch_id"
    )
    for batch_id, content in batches:
        parsed = parse_capture(bytes(content))
        if parsed.batch_id != str(batch_id):
            problems.append(f"batch {batch_id}: evidence derives id {parsed.batch_id}")
        expected = {
            stable_id(
                "observation",
                parsed.source_id,
                r.source_record_id,
                iso(r.observed_time),
                r.content_sha256,
            )
            for r in parsed.records
        }
        stored = {
            str(row[0])
            for row in conn.run(
                "SELECT observation_id FROM eye.observation "
                "WHERE observation_id = ANY(CAST(:ids AS uuid[]))",
                ids=sorted(expected),
            )
        }
        missing = expected - stored
        if missing:
            problems.append(f"batch {batch_id}: {len(missing)} derived observation(s) missing")
        coverage_id = stable_id("coverage", parsed.batch_id, parsed.layer, METRIC_NAME)
        if not conn.run("SELECT 1 FROM eye.coverage WHERE coverage_id = :id", id=coverage_id):
            problems.append(f"batch {batch_id}: coverage row missing")
    return problems
