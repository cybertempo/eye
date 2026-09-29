-- 0005: append-only event-claim ledger (Package 4c).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 to 0004 are unchanged. Adds tables and a view; no
--   existing row is read or rewritten. Expected lock impact: none on existing
--   tables (new objects only).
-- * A claim is one version of a source's case, exactly as published: source,
--   case id, basis, kind, status, event time and its uncertainty, publication
--   time, reported geometry and precision, evidence reference, subject
--   identifiers and window. Its id is derived from source, layer, case id and
--   content, so a duplicate delivery adds a receipt, not a claim.
-- * The reported event location is the only geometry stored here. A last
--   observed position is never stored with a claim: it is read from
--   observations when a claim is shown, so the two cannot be confused.
-- * Corrections and retractions are later claims for the same case, ordered by
--   the source's publication time (never by load order). Claims published at
--   the same time with different content are a conflict: the current version
--   is unknown (NULL), never chosen.
-- * Motion data proves nothing: a claim whose basis is motion inference (a lost
--   signal, an AIS gap, a stopped vessel, a traffic slowdown) can only be a
--   review candidate, and a candidate kind can only come from motion
--   inference. An accident, casualty or collision needs a sourced report with
--   an evidence reference. The capture parser refuses these combinations
--   first; the constraints below refuse them again.
-- * Claims and receipts are append-only.

CREATE TYPE eye.claim_basis AS ENUM ('official_report', 'operator_report', 'motion_inference');
CREATE TYPE eye.claim_status AS ENUM
    ('reported', 'preliminary', 'final', 'retracted', 'cleared', 'candidate');

CREATE TABLE eye.event_claim (
    claim_id                 uuid PRIMARY KEY,
    source_id                eye.identifier NOT NULL,
    layer                    eye.identifier NOT NULL CHECK (layer IN ('flight', 'vessel', 'road')),
    case_id                  eye.identifier NOT NULL,
    basis                    eye.claim_basis NOT NULL,
    kind                     eye.identifier NOT NULL,
    status                   eye.claim_status NOT NULL,
    event_time               timestamptz,
    event_time_uncertainty_s integer CHECK (event_time_uncertainty_s BETWEEN 0 AND 2592000),
    source_published_time    timestamptz NOT NULL,
    location_type            text NOT NULL CHECK (location_type IN ('point', 'segment', 'area')),
    location                 geometry(Geometry, 4326) NOT NULL,
    precision_m              double precision NOT NULL CHECK (precision_m BETWEEN 0 AND 1000000),
    segment_direction        text CHECK (segment_direction IN ('forward', 'both')),
    evidence_ref             text CHECK (evidence_ref IS NULL OR length(evidence_ref) BETWEEN 1 AND 200),
    subject_identifiers      text[] NOT NULL DEFAULT '{}'
                             CHECK (cardinality(subject_identifiers) <= 10),
    subject_window_start     timestamptz,
    subject_window_end       timestamptz,
    summary                  text CHECK (summary IS NULL OR length(summary) BETWEEN 1 AND 500),
    content_sha256           eye.sha256_hex NOT NULL,
    CONSTRAINT claim_kind_of_layer CHECK (
        (layer = 'flight' AND kind IN ('aviation_accident', 'aviation_incident', 'emergency_declared',
                                       'diversion', 'flight_arrival', 'signal_lost'))
        OR (layer = 'vessel' AND kind IN ('marine_casualty', 'vessel_distress', 'vessel_port_arrival',
                                          'ais_gap', 'vessel_stopped'))
        OR (layer = 'road' AND kind IN ('road_collision', 'road_incident', 'road_closure',
                                        'road_congestion', 'traffic_slowdown'))),
    -- Motion inference is only ever a review candidate, only of a motion
    -- kind, and cites no report; a sourced report is never a candidate and
    -- always cites its evidence.
    CONSTRAINT motion_inference_is_only_a_candidate CHECK (
        (basis = 'motion_inference') = (status = 'candidate')
        AND (basis = 'motion_inference')
            = (kind IN ('signal_lost', 'ais_gap', 'vessel_stopped', 'traffic_slowdown'))
        AND (basis = 'motion_inference') = (evidence_ref IS NULL)),
    CHECK ((event_time IS NULL) = (event_time_uncertainty_s IS NULL)),
    -- A source cannot publish a report before the earliest time the event could have happened.
    CHECK (event_time IS NULL
           OR source_published_time >= event_time - make_interval(secs => event_time_uncertainty_s)),
    CHECK ((subject_window_start IS NULL) = (subject_window_end IS NULL)),
    CHECK (subject_window_end IS NULL OR subject_window_end > subject_window_start),
    CONSTRAINT claim_geometry_matches_type CHECK (
        (location_type = 'point' AND GeometryType(location) = 'POINT')
        OR (location_type = 'segment' AND GeometryType(location) = 'LINESTRING')
        OR (location_type = 'area' AND GeometryType(location) = 'POLYGON')),
    CHECK ((segment_direction IS NULL) = (location_type <> 'segment')),
    CHECK (ST_XMin(location) >= -180 AND ST_XMax(location) <= 180
           AND ST_YMin(location) >= -90 AND ST_YMax(location) <= 90)
);
COMMENT ON COLUMN eye.event_claim.location IS
    'Reported event location, as the source published it. Never a last observed position.';
COMMENT ON COLUMN eye.event_claim.source_published_time IS
    'When the source issued this version of the case; orders corrections and retractions.';
CREATE INDEX event_claim_case_idx ON eye.event_claim (source_id, layer, case_id, source_published_time);
CREATE INDEX event_claim_location_idx ON eye.event_claim USING gist (location);

-- Each delivery of a claim, like eye.observation_receipt.
CREATE TABLE eye.event_claim_receipt (
    claim_id        uuid NOT NULL REFERENCES eye.event_claim (claim_id),
    batch_id        uuid NOT NULL REFERENCES eye.capture_batch (batch_id),
    evidence_id     uuid NOT NULL,
    received_time   timestamptz NOT NULL,
    adapter_version text NOT NULL,
    PRIMARY KEY (claim_id, batch_id),
    CONSTRAINT claim_receipt_evidence_of_batch FOREIGN KEY (evidence_id, batch_id)
        REFERENCES eye.raw_evidence (evidence_id, batch_id)
);
CREATE INDEX event_claim_receipt_batch_idx ON eye.event_claim_receipt (batch_id);

CREATE FUNCTION eye.check_claim_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    finished timestamptz;
    published timestamptz;
BEGIN
    SELECT attempt_finished_at INTO finished FROM eye.capture_batch WHERE batch_id = NEW.batch_id;
    SELECT source_published_time INTO published FROM eye.event_claim WHERE claim_id = NEW.claim_id;
    IF NEW.received_time IS DISTINCT FROM finished THEN
        RAISE EXCEPTION 'eye.event_claim_receipt: received_time % is not the batch receipt time %',
            NEW.received_time, finished USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.received_time < published THEN
        RAISE EXCEPTION 'eye.event_claim_receipt: received % before the source published it %',
            NEW.received_time, published USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER event_claim_receipt_chronology BEFORE INSERT ON eye.event_claim_receipt
    FOR EACH ROW EXECUTE FUNCTION eye.check_claim_receipt();

CREATE TRIGGER event_claim_append_only BEFORE UPDATE OR DELETE ON eye.event_claim
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
CREATE TRIGGER event_claim_receipt_append_only BEFORE UPDATE OR DELETE ON eye.event_claim_receipt
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- Versions of one case (source, layer, case id), ranked by publication time;
-- same rules as eye.observation_version. Derived, so it cannot drift.
CREATE VIEW eye.event_claim_version AS
WITH receipts AS (
    SELECT claim_id, min(received_time) AS first_received_time, count(*) AS receipt_count
    FROM eye.event_claim_receipt GROUP BY claim_id
), ranked AS (
    SELECT c.claim_id, c.source_id, c.layer, c.case_id, c.source_published_time,
           r.first_received_time, r.receipt_count,
           dense_rank() OVER (PARTITION BY c.source_id, c.layer, c.case_id
                              ORDER BY c.source_published_time) AS version,
           count(*) OVER (PARTITION BY c.source_id, c.layer, c.case_id,
                          c.source_published_time) AS tier_size,
           max(c.source_published_time) OVER (PARTITION BY c.source_id, c.layer, c.case_id)
               AS latest_published
    FROM eye.event_claim c JOIN receipts r USING (claim_id)
)
SELECT v.claim_id, v.source_id, v.layer, v.case_id, v.source_published_time,
       v.first_received_time, v.receipt_count, v.version,
       CASE WHEN v.tier_size = 1 THEN p.claim_id END AS supersedes_claim_id,
       CASE WHEN v.source_published_time < v.latest_published THEN false
            WHEN v.tier_size = 1 THEN true
            END AS is_current,
       v.tier_size > 1 AS publication_conflict
FROM ranked v
LEFT JOIN ranked p
       ON p.source_id = v.source_id AND p.layer = v.layer AND p.case_id = v.case_id
      AND p.version = v.version - 1 AND p.tier_size = 1;
COMMENT ON VIEW eye.event_claim_version IS
    'is_current NULL: the latest publication time holds conflicting claims; unknown, not chosen.';
