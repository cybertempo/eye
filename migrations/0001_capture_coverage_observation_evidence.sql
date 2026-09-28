-- 0001: capture batches, raw evidence, observations and coverage.
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only. The runner applies this file in one transaction with the
--   version row, so a failure leaves the database exactly as it was.
-- * Source event time (observation.observed_time), source publication time and
--   EYE receipt time (observation_receipt.received_time, stamped by EYE's capture
--   adapter) are separate NOT NULL columns in separate rows and never merged.
-- * Correction order comes from the source's publication time, not load order.
-- * Coverage in state unknown/failed must have a NULL metric; a zero is only
--   valid for qualified/partial coverage (brief section 4).
-- * Raw evidence, observations and coverage are append-only; retention and
--   deletion are Package 5 and must pass the checked backup transition first.
-- * No TimescaleDB feature is used; plain PostgreSQL + PostGIS only.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA eye;

CREATE DOMAIN eye.identifier AS text
    CHECK (VALUE ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$');

CREATE DOMAIN eye.sha256_hex AS text
    CHECK (VALUE ~ '^[0-9a-f]{64}$');

-- Canonical text form of an instant, used wherever stored times are compared
-- or exported: RFC 3339, UTC, microseconds, Z suffix.
CREATE FUNCTION eye.iso_utc(t timestamptz) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
$$;

CREATE TYPE eye.coverage_state AS ENUM ('qualified', 'partial', 'unknown', 'failed');
CREATE TYPE eye.batch_status AS ENUM ('pending', 'committed', 'failed');
CREATE TYPE eye.display_type AS ENUM ('observed', 'ephemeris', 'propagated', 'estimated', 'illustrative');

-- One attempted capture. Created as 'pending' together with its raw evidence;
-- moved to 'committed' or 'failed' once parsed records are stored.
CREATE TABLE eye.capture_batch (
    batch_id            uuid PRIMARY KEY,
    source_id           eye.identifier NOT NULL,
    layer               eye.identifier NOT NULL,
    adapter_version     text NOT NULL CHECK (length(adapter_version) BETWEEN 1 AND 64),
    capture_format      text NOT NULL CHECK (length(capture_format) BETWEEN 1 AND 64),
    requested_area      geometry(Polygon, 4326) NOT NULL,
    requested_start     timestamptz NOT NULL,
    requested_end       timestamptz NOT NULL,
    expected_interval_s integer NOT NULL CHECK (expected_interval_s > 0),
    attempt_started_at  timestamptz NOT NULL,
    attempt_finished_at timestamptz NOT NULL,
    provider_status     text NOT NULL CHECK (provider_status IN ('ok', 'error', 'timeout', 'indeterminate')),
    quota_cost          numeric NOT NULL CHECK (quota_cost >= 0),
    evidence_sha256     eye.sha256_hex NOT NULL UNIQUE,
    status              eye.batch_status NOT NULL DEFAULT 'pending',
    accepted_count      integer CHECK (accepted_count >= 0),
    rejected_count      integer CHECK (rejected_count >= 0),
    failure_reason      text CHECK (failure_reason IS NULL OR length(failure_reason) <= 500),
    archived_at         timestamptz NOT NULL DEFAULT now(),
    committed_at        timestamptz,
    CHECK (requested_end > requested_start),
    CHECK (attempt_finished_at >= attempt_started_at),
    -- EYE cannot have received data about an interval that had not ended.
    CHECK (attempt_finished_at >= requested_end),
    CHECK (status = 'pending' OR (accepted_count IS NOT NULL AND rejected_count IS NOT NULL
                                  AND committed_at IS NOT NULL))
);

-- The exact bytes received, bounded, with checksum. One row per batch.
CREATE TABLE eye.raw_evidence (
    evidence_id  uuid PRIMARY KEY,
    batch_id     uuid NOT NULL UNIQUE REFERENCES eye.capture_batch (batch_id),
    sha256       eye.sha256_hex NOT NULL UNIQUE,
    byte_size    integer NOT NULL CHECK (byte_size BETWEEN 1 AND 1048576),
    media_type   text NOT NULL CHECK (media_type = 'application/json'),
    content      bytea NOT NULL,
    CHECK (octet_length(content) = byte_size)
);

-- One observed state as the source published it: content only. The id is
-- derived from source, record, observed time, source publication time and
-- content, so rebuilds and replays reproduce it. Receipts live separately.
CREATE TABLE eye.observation (
    observation_id        uuid PRIMARY KEY,
    source_id             eye.identifier NOT NULL,
    source_record_id      eye.identifier NOT NULL,
    layer                 eye.identifier NOT NULL,
    display_type          eye.display_type NOT NULL DEFAULT 'observed',
    observed_time         timestamptz NOT NULL,
    source_published_time timestamptz NOT NULL,
    position              geometry(Point, 4326) NOT NULL,
    altitude_m            double precision CHECK (altitude_m BETWEEN -12000 AND 100000),
    confidence            double precision CHECK (confidence BETWEEN 0 AND 1),
    quality_flags         text[] NOT NULL DEFAULT '{}' CHECK (cardinality(quality_flags) <= 16),
    content_sha256        eye.sha256_hex NOT NULL,
    CHECK (ST_X(position) BETWEEN -180 AND 180 AND ST_Y(position) BETWEEN -90 AND 90),
    -- A source cannot publish an observation before it happened.
    CHECK (source_published_time >= observed_time)
);
COMMENT ON COLUMN eye.observation.observed_time IS
    'Source event time: when the source says the state was observed. Never a receipt time.';
COMMENT ON COLUMN eye.observation.source_published_time IS
    'When the source issued this version of the record; orders corrections.';
CREATE INDEX observation_record_idx
    ON eye.observation (source_id, source_record_id, observed_time, source_published_time);
CREATE INDEX observation_time_idx ON eye.observation (observed_time);
CREATE INDEX observation_position_idx ON eye.observation USING gist (position);

-- Each delivery of an observation to EYE. received_time is stamped by EYE's
-- capture adapter when the provider response arrived (the batch's
-- attempt_finished_at); a provider can never supply it. A duplicate delivery
-- adds a receipt, not an observation.
CREATE TABLE eye.observation_receipt (
    observation_id  uuid NOT NULL REFERENCES eye.observation (observation_id),
    batch_id        uuid NOT NULL REFERENCES eye.capture_batch (batch_id),
    evidence_id     uuid NOT NULL REFERENCES eye.raw_evidence (evidence_id),
    received_time   timestamptz NOT NULL,
    schema_version  text NOT NULL,
    adapter_version text NOT NULL,
    PRIMARY KEY (observation_id, batch_id)
);
COMMENT ON COLUMN eye.observation_receipt.received_time IS
    'EYE receipt time, stamped by the capture adapter; equals the batch attempt_finished_at.';
CREATE INDEX observation_receipt_batch_idx ON eye.observation_receipt (batch_id);

-- Receipt chronology is enforced, not assumed.
CREATE FUNCTION eye.check_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    finished timestamptz;
    published timestamptz;
BEGIN
    SELECT attempt_finished_at INTO finished FROM eye.capture_batch WHERE batch_id = NEW.batch_id;
    SELECT source_published_time INTO published
        FROM eye.observation WHERE observation_id = NEW.observation_id;
    IF NEW.received_time IS DISTINCT FROM finished THEN
        RAISE EXCEPTION 'eye.observation_receipt: received_time % is not the batch receipt time %',
            NEW.received_time, finished USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.received_time < published THEN
        RAISE EXCEPTION 'eye.observation_receipt: received % before the source published it %',
            NEW.received_time, published USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER observation_receipt_chronology BEFORE INSERT ON eye.observation_receipt
    FOR EACH ROW EXECUTE FUNCTION eye.check_receipt();

-- Versions of one source record at one observed time, ordered by the source's
-- publication time (then id), never by load order. Derived, so it cannot drift.
CREATE VIEW eye.observation_version AS
SELECT o.observation_id,
       o.source_id,
       o.source_record_id,
       o.observed_time,
       o.source_published_time,
       r.first_received_time,
       r.receipt_count,
       row_number() OVER w AS version,
       lag(o.observation_id) OVER w AS supersedes_observation_id,
       lead(o.observation_id) OVER w IS NULL AS is_current,
       count(*) OVER (PARTITION BY o.source_id, o.source_record_id, o.observed_time,
                      o.source_published_time) > 1 AS publication_conflict
FROM eye.observation o
JOIN (SELECT observation_id, min(received_time) AS first_received_time, count(*) AS receipt_count
      FROM eye.observation_receipt GROUP BY observation_id) r USING (observation_id)
WINDOW w AS (PARTITION BY o.source_id, o.source_record_id, o.observed_time
             ORDER BY o.source_published_time, o.observation_id);
COMMENT ON VIEW eye.observation_version IS
    'publication_conflict: two different versions share a publication time; neither is authoritative.';

-- Coverage for one batch and layer. Missing data is NULL, never zero.
CREATE TABLE eye.coverage (
    coverage_id    uuid PRIMARY KEY,
    batch_id       uuid NOT NULL REFERENCES eye.capture_batch (batch_id),
    source_id      eye.identifier NOT NULL,
    layer          eye.identifier NOT NULL,
    interval_start timestamptz NOT NULL,
    interval_end   timestamptz NOT NULL,
    state          eye.coverage_state NOT NULL,
    reason         text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 500),
    metric_name    eye.identifier NOT NULL,
    metric_value   numeric CHECK (metric_value >= 0),
    derivation_version text NOT NULL,
    CHECK (interval_end > interval_start),
    CONSTRAINT coverage_missing_is_null CHECK (
        (state IN ('unknown', 'failed') AND metric_value IS NULL AND reason IS NOT NULL)
        OR (state IN ('qualified', 'partial') AND metric_value IS NOT NULL)
    ),
    UNIQUE (batch_id, layer, metric_name)
);

-- Append-only history: no UPDATE or DELETE on evidence, observations or coverage.
CREATE FUNCTION eye.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'eye.%: % is not permitted; history is append-only', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER raw_evidence_append_only BEFORE UPDATE OR DELETE ON eye.raw_evidence
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
CREATE TRIGGER observation_append_only BEFORE UPDATE OR DELETE ON eye.observation
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
CREATE TRIGGER observation_receipt_append_only BEFORE UPDATE OR DELETE ON eye.observation_receipt
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
CREATE TRIGGER coverage_append_only BEFORE UPDATE OR DELETE ON eye.coverage
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- A batch may only move pending -> committed or pending -> failed, and its
-- capture facts never change.
CREATE FUNCTION eye.guard_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'eye.capture_batch: DELETE is not permitted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'eye.capture_batch: batch % is already %', OLD.batch_id, OLD.status
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (NEW.batch_id, NEW.source_id, NEW.layer, NEW.adapter_version, NEW.capture_format,
        NEW.requested_start, NEW.requested_end, NEW.expected_interval_s,
        NEW.attempt_started_at, NEW.attempt_finished_at, NEW.provider_status,
        NEW.quota_cost, NEW.evidence_sha256, NEW.archived_at)
       IS DISTINCT FROM
       (OLD.batch_id, OLD.source_id, OLD.layer, OLD.adapter_version, OLD.capture_format,
        OLD.requested_start, OLD.requested_end, OLD.expected_interval_s,
        OLD.attempt_started_at, OLD.attempt_finished_at, OLD.provider_status,
        OLD.quota_cost, OLD.evidence_sha256, OLD.archived_at)
       OR NOT ST_Equals(NEW.requested_area, OLD.requested_area) THEN
        RAISE EXCEPTION 'eye.capture_batch: capture facts are immutable'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER capture_batch_guard BEFORE UPDATE OR DELETE ON eye.capture_batch
    FOR EACH ROW EXECUTE FUNCTION eye.guard_batch();
