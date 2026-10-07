-- 0007: derivation manifests, replayable rollups, synthetic backup proofs and
-- the checked retention transition (Package 5, lifecycle slice).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 to 0006 are unchanged.
-- * A manifest records one derivation of one partition day: its input batch
--   set and checksum, watermark, lateness deadline, required derivation
--   versions, output row count and checksum, and coverage summary. Manifests
--   and their rollup rows are append-only; a late arrival or correction adds a
--   new manifest and keeps the old one. The current manifest is the latest
--   one per key (eye.current_manifest).
-- * Rollup rows obey the coverage rules: unknown/failed has a NULL value and a
--   reason; partial is a lower bound with a reason; only qualified is exact.
-- * Backup proofs here are synthetic code tests only (target class
--   'synthetic-directory', synthetic = true). They prove nothing about an
--   off-site backup, a private installation or a recovery objective.
-- * Retention prunes raw evidence BYTES only. The raw_evidence row keeps its
--   id, checksum, size and media type and gains the id of the retention
--   decision that pruned it. The append-only trigger on eye.raw_evidence is
--   replaced by a guard that refuses DELETE and every UPDATE except one:
--   setting content to NULL, in the same transaction as a decision with
--   verdict 'pruned' that names the batch, after the partition's lateness
--   window by the database clock, citing every required current manifest
--   (each complete and checked valid in that transaction) and a verified
--   synthetic backup proof holding exactly these bytes and manifests
--   (eye.pruning_refusal). Observations, receipts, event
--   claims, media items, coverage and derivation history keep their
--   append-only triggers; there is no deletion path for them.
-- * Expected lock impact: ALTER TABLE eye.raw_evidence takes an ACCESS
--   EXCLUSIVE lock for the duration of this migration (no table rewrite: the
--   new column is nullable without a default, and the replacement CHECK only
--   scans). New tables are created empty.

CREATE TYPE eye.manifest_state AS ENUM ('valid', 'stale', 'failed', 'unverified');

CREATE TABLE eye.derivation_manifest (
    manifest_id        uuid PRIMARY KEY,
    manifest_seq       bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    source_id          eye.identifier NOT NULL,
    layer              eye.identifier NOT NULL,
    day                date NOT NULL,
    interval_start     timestamptz NOT NULL,
    interval_end       timestamptz NOT NULL,
    derivation         text NOT NULL CHECK (derivation IN (
                           'coverage-hourly', 'observations-hourly', 'cells-daily',
                           'transit-daily', 'ledger-replay')),
    derivation_version text NOT NULL CHECK (length(derivation_version) BETWEEN 1 AND 64),
    -- '' for a whole source and layer; 'line:<line_id>/v<version>' for transits.
    scope              text NOT NULL CHECK (length(scope) <= 200),
    required_versions  jsonb NOT NULL CHECK (jsonb_typeof(required_versions) = 'object'),
    input_batch_ids    uuid[] NOT NULL,
    input_batch_count  integer NOT NULL CHECK (input_batch_count = cardinality(input_batch_ids)),
    input_row_count    integer NOT NULL CHECK (input_row_count >= 0),
    input_sha256       eye.sha256_hex NOT NULL,
    -- Latest archive time among the input batches (NULL when there are none).
    -- Informational: it is not part of the manifest id, so a restore that
    -- archives the same evidence later reproduces the same id.
    watermark          timestamptz,
    lateness_deadline  timestamptz NOT NULL,
    transit_run_id     uuid REFERENCES eye.derivation_run (run_id),
    output_row_count   integer NOT NULL CHECK (output_row_count >= 0),
    output_sha256      eye.sha256_hex NOT NULL,
    coverage_summary   jsonb NOT NULL CHECK (jsonb_typeof(coverage_summary) = 'object'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (interval_start = (day::timestamp AT TIME ZONE 'UTC')),
    CHECK (interval_end = interval_start + interval '1 day'),
    CHECK (lateness_deadline >= interval_end),
    CHECK ((derivation = 'transit-daily') = (transit_run_id IS NOT NULL)),
    CHECK ((derivation = 'transit-daily') = (scope LIKE 'line:%'))
);
CREATE INDEX derivation_manifest_key_idx
    ON eye.derivation_manifest (source_id, layer, day, derivation, scope, manifest_seq);
CREATE TRIGGER derivation_manifest_append_only BEFORE UPDATE OR DELETE ON eye.derivation_manifest
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- Rollup rows of one manifest. A value is a count or a duration; an
-- unknown or failed row has none. No row carries a coordinate: a cell is
-- named by its id and bounds, never by a mean or centre position.
CREATE TABLE eye.rollup_value (
    manifest_id    uuid NOT NULL REFERENCES eye.derivation_manifest (manifest_id),
    row_key        text NOT NULL CHECK (row_key ~ '^[a-z]+(:[A-Za-z0-9._/-]+)*$'
                                        AND length(row_key) <= 100),
    interval_start timestamptz NOT NULL,
    interval_end   timestamptz NOT NULL,
    metric         eye.identifier NOT NULL,
    state          eye.coverage_state NOT NULL,
    value          numeric CHECK (value >= 0),
    reason         text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 500),
    detail         jsonb NOT NULL CHECK (jsonb_typeof(detail) = 'object'),
    PRIMARY KEY (manifest_id, row_key),
    CHECK (interval_end > interval_start),
    CONSTRAINT rollup_missing_is_null CHECK (
        (state IN ('unknown', 'failed') AND value IS NULL AND reason IS NOT NULL)
        OR (state IN ('qualified', 'partial') AND value IS NOT NULL)
    ),
    CONSTRAINT rollup_partial_has_reason CHECK (state <> 'partial' OR reason IS NOT NULL),
    CONSTRAINT rollup_has_no_position CHECK (
        NOT (detail ?| ARRAY['lon', 'lat', 'position', 'centroid', 'mean_lon', 'mean_lat'])
    )
);
CREATE TRIGGER rollup_value_append_only BEFORE UPDATE OR DELETE ON eye.rollup_value
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- The latest manifest per (source, layer, day, derivation, scope). Older
-- manifests stay in eye.derivation_manifest as correction history.
CREATE VIEW eye.current_manifest AS
SELECT DISTINCT ON (source_id, layer, day, derivation, scope) *
FROM eye.derivation_manifest
ORDER BY source_id, layer, day, derivation, scope, manifest_seq DESC;

CREATE VIEW eye.current_rollup AS
SELECT m.source_id, m.layer, m.day, m.derivation, m.derivation_version, m.scope,
       r.manifest_id, r.row_key, r.interval_start, r.interval_end, r.metric, r.state,
       r.value, r.reason, r.detail
FROM eye.current_manifest m JOIN eye.rollup_value r USING (manifest_id);

-- Each time a manifest is checked against current evidence. A check that
-- could not run is 'unverified', never 'valid'.
CREATE TABLE eye.manifest_validation (
    validation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    manifest_id   uuid NOT NULL REFERENCES eye.derivation_manifest (manifest_id),
    state         eye.manifest_state NOT NULL,
    reason        text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 500),
    checked_at    timestamptz NOT NULL DEFAULT now(),
    -- The transaction that recorded the check; the pruning guard requires a
    -- valid check of every cited manifest in the pruning transaction itself.
    txid          bigint NOT NULL DEFAULT txid_current(),
    CHECK ((state = 'valid') = (reason IS NULL))
);
CREATE INDEX manifest_validation_manifest_idx ON eye.manifest_validation (manifest_id, checked_at);
CREATE TRIGGER manifest_validation_append_only BEFORE UPDATE OR DELETE ON eye.manifest_validation
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- One verification of one backup generation by an independent read-back.
-- Synthetic only in the public repository.
CREATE TABLE eye.backup_proof (
    proof_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    target_class      text NOT NULL CHECK (target_class = 'synthetic-directory'),
    synthetic         boolean NOT NULL CHECK (synthetic),
    generation_id     eye.sha256_hex NOT NULL,
    generation_sha256 eye.sha256_hex,
    batch_ids         uuid[] NOT NULL,
    evidence_sha256s  text[] NOT NULL CHECK (cardinality(evidence_sha256s) = cardinality(batch_ids)),
    manifest_ids      uuid[] NOT NULL,
    state             text NOT NULL CHECK (state IN ('verified', 'failed')),
    reason            text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 500),
    verifier_version  text NOT NULL CHECK (length(verifier_version) BETWEEN 1 AND 64),
    verified_at       timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'verified') = (reason IS NULL)),
    CHECK (state <> 'verified' OR generation_sha256 IS NOT NULL)
);
CREATE TRIGGER backup_proof_append_only BEFORE UPDATE OR DELETE ON eye.backup_proof
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- Every executed retention decision, refused ones included. A plan is
-- read-only and records nothing.
CREATE TABLE eye.retention_decision (
    decision_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       eye.identifier NOT NULL,
    layer           eye.identifier NOT NULL,
    partition_day   date NOT NULL,
    verdict         text NOT NULL CHECK (verdict IN ('pruned', 'blocked', 'unverified')),
    reasons         jsonb NOT NULL CHECK (jsonb_typeof(reasons) = 'array'),
    batch_ids       uuid[] NOT NULL,
    manifest_ids    uuid[] NOT NULL,
    backup_proof_id uuid REFERENCES eye.backup_proof (proof_id),
    evaluated_at    timestamptz NOT NULL,
    -- The lateness window the decision applied; never below the 48 hour floor
    -- the pruning guard enforces with the database clock.
    lateness_hours  integer NOT NULL CHECK (lateness_hours BETWEEN 48 AND 8760),
    decided_at      timestamptz NOT NULL DEFAULT now(),
    txid            bigint NOT NULL DEFAULT txid_current(),
    CHECK ((verdict = 'pruned') = (jsonb_array_length(reasons) = 0)),
    CHECK (verdict <> 'pruned' OR (backup_proof_id IS NOT NULL AND cardinality(batch_ids) > 0))
);
CREATE TRIGGER retention_decision_append_only BEFORE UPDATE OR DELETE ON eye.retention_decision
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- Which backup proof each manifest relied on when its partition was pruned.
CREATE VIEW eye.manifest_retention_proof AS
SELECT unnest(d.manifest_ids) AS manifest_id, d.decision_id, d.backup_proof_id,
       d.source_id, d.layer, d.partition_day, d.decided_at
FROM eye.retention_decision d
WHERE d.verdict = 'pruned';

-- Raw evidence may lose its bytes, and only its bytes, through a recorded
-- pruning decision. The table-level CHECK created by 0001 is replaced by one
-- that allows NULL content.
ALTER TABLE eye.raw_evidence DROP CONSTRAINT raw_evidence_check;
ALTER TABLE eye.raw_evidence ALTER COLUMN content DROP NOT NULL;
ALTER TABLE eye.raw_evidence ADD CONSTRAINT raw_evidence_size_matches
    CHECK (content IS NULL OR octet_length(content) = byte_size);
ALTER TABLE eye.raw_evidence ADD COLUMN pruned_by_decision uuid
    REFERENCES eye.retention_decision (decision_id);
ALTER TABLE eye.raw_evidence ADD CONSTRAINT raw_evidence_pruned_has_decision
    CHECK ((content IS NULL) = (pruned_by_decision IS NOT NULL));
COMMENT ON COLUMN eye.raw_evidence.pruned_by_decision IS
    'Set only when retention pruned the bytes; the decision cites the verified backup holding them.';

-- Why pruning one batch's bytes under one decision is refused, or NULL when
-- every condition the database can check independently holds. It does not
-- re-derive anything (the coordinator does); it checks recorded facts with
-- the database clock:
-- * a 'pruned' decision written in this transaction naming the batch and its
--   partition, which holds no pending batch;
-- * the partition's lateness window (day end + the decision's lateness, at
--   least 48 hours) has closed by now();
-- * a verified synthetic proof holding these exact bytes and every cited
--   manifest, and no later failed backup verification;
-- * for every day the batch's request overlaps: a current coverage manifest,
--   and for position captures current observation and cell manifests; for the
--   batch's own day a current ledger-replay manifest; every current manifest
--   of those days cited by the decision;
-- * every cited manifest is current, includes every settled batch it should
--   (so a late arrival leaves it incomplete), and was checked 'valid', and
--   never otherwise, in this transaction.
CREATE FUNCTION eye.pruning_refusal(decision uuid, batch uuid, bytes_sha256 text)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    d eye.retention_decision;
    b eye.capture_batch;
    p eye.backup_proof;
    deadline timestamptz;
    m record;
    want text;
    days date[];
BEGIN
    SELECT * INTO d FROM eye.retention_decision WHERE decision_id = decision;
    IF NOT FOUND OR d.verdict <> 'pruned' THEN
        RETURN 'no pruned decision';
    END IF;
    IF d.txid <> txid_current() THEN
        RETURN 'the decision was not written in this transaction';
    END IF;
    SELECT * INTO b FROM eye.capture_batch WHERE batch_id = batch;
    IF NOT (batch = ANY (d.batch_ids)) OR b.status = 'pending'
       OR b.source_id <> d.source_id OR b.layer <> d.layer
       OR (b.requested_start AT TIME ZONE 'UTC')::date <> d.partition_day THEN
        RETURN 'the decision does not name this settled batch and its partition';
    END IF;
    IF EXISTS (SELECT 1 FROM eye.capture_batch x WHERE x.source_id = d.source_id
               AND x.layer = d.layer AND x.status = 'pending'
               AND (x.requested_start AT TIME ZONE 'UTC')::date = d.partition_day) THEN
        RETURN 'the partition holds a pending batch';
    END IF;
    deadline := ((d.partition_day + 1)::timestamp AT TIME ZONE 'UTC')
                + make_interval(hours => d.lateness_hours);
    IF now() < deadline THEN
        RETURN format('the lateness window is open until %s', deadline);
    END IF;
    SELECT * INTO p FROM eye.backup_proof WHERE proof_id = d.backup_proof_id;
    IF NOT FOUND OR p.state <> 'verified' OR NOT p.synthetic
       OR NOT EXISTS (SELECT 1 FROM unnest(p.batch_ids, p.evidence_sha256s) AS h (bid, sha)
                      WHERE h.bid = batch AND h.sha = bytes_sha256)
       OR NOT (d.manifest_ids <@ p.manifest_ids) THEN
        RETURN 'no verified backup proof holds these bytes and every cited manifest';
    END IF;
    IF EXISTS (SELECT 1 FROM eye.backup_proof f WHERE f.state = 'failed'
               AND f.verified_at > p.verified_at) THEN
        RETURN 'a later backup verification failed';
    END IF;
    SELECT array_agg(g::date) INTO days
    FROM generate_series((b.requested_start AT TIME ZONE 'UTC')::date,
                         ((b.requested_end - interval '1 microsecond') AT TIME ZONE 'UTC')::date,
                         interval '1 day') g;
    FOR m IN
        SELECT t.day, w.derivation FROM unnest(days) t (day)
        CROSS JOIN LATERAL (VALUES ('coverage-hourly'), ('observations-hourly'),
                                   ('cells-daily'), ('ledger-replay')) w (derivation)
        WHERE (w.derivation <> 'ledger-replay' OR t.day = d.partition_day)
          AND (w.derivation NOT IN ('observations-hourly', 'cells-daily')
               OR b.capture_format = 'eye.synthetic-capture/2')
    LOOP
        IF NOT EXISTS (SELECT 1 FROM eye.current_manifest c WHERE c.source_id = d.source_id
                       AND c.layer = d.layer AND c.day = m.day
                       AND c.derivation = m.derivation
                       AND c.manifest_id = ANY (d.manifest_ids)) THEN
            RETURN format('no current %s manifest of %s is cited', m.derivation, m.day);
        END IF;
    END LOOP;
    SELECT format('current %s manifest of %s is not cited', c.derivation, c.day) INTO want
    FROM eye.current_manifest c
    WHERE c.source_id = d.source_id AND c.layer = d.layer AND c.day = ANY (days)
      AND (c.derivation <> 'ledger-replay' OR c.day = d.partition_day)
      AND NOT (c.manifest_id = ANY (d.manifest_ids))
    LIMIT 1;
    IF want IS NOT NULL THEN
        RETURN want;
    END IF;
    FOR m IN
        SELECT c.* FROM eye.derivation_manifest c WHERE c.manifest_id = ANY (d.manifest_ids)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM eye.current_manifest c WHERE c.manifest_id = m.manifest_id) THEN
            RETURN format('cited manifest %s is no longer current', m.manifest_id);
        END IF;
        IF EXISTS (
            SELECT 1 FROM eye.capture_batch x
            WHERE x.source_id = m.source_id AND x.layer = m.layer AND x.status <> 'pending'
              AND CASE WHEN m.derivation = 'ledger-replay'
                       THEN (x.requested_start AT TIME ZONE 'UTC')::date = m.day
                       ELSE x.requested_start < m.interval_end
                            AND x.requested_end > m.interval_start END
              AND NOT (x.batch_id = ANY (m.input_batch_ids))
        ) THEN
            RETURN format('cited %s manifest of %s misses a settled batch (a late arrival)',
                          m.derivation, m.day);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM eye.manifest_validation v
                       WHERE v.manifest_id = m.manifest_id AND v.txid = txid_current()
                         AND v.state = 'valid')
           OR EXISTS (SELECT 1 FROM eye.manifest_validation v
                      WHERE v.manifest_id = m.manifest_id AND v.txid = txid_current()
                        AND v.state <> 'valid') THEN
            RETURN format('cited %s manifest of %s was not checked valid in this transaction',
                          m.derivation, m.day);
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE FUNCTION eye.guard_raw_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    refusal text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'eye.raw_evidence: DELETE is not permitted; history is append-only'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.content IS NULL THEN
        RAISE EXCEPTION 'eye.raw_evidence: evidence % is already pruned', OLD.evidence_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.content IS NOT NULL OR NEW.pruned_by_decision IS NULL
       OR (NEW.evidence_id, NEW.batch_id, NEW.sha256, NEW.byte_size, NEW.media_type)
          IS DISTINCT FROM
          (OLD.evidence_id, OLD.batch_id, OLD.sha256, OLD.byte_size, OLD.media_type) THEN
        RAISE EXCEPTION 'eye.raw_evidence: UPDATE is not permitted except pruning the bytes'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    refusal := eye.pruning_refusal(NEW.pruned_by_decision, OLD.batch_id, OLD.sha256);
    IF refusal IS NOT NULL THEN
        RAISE EXCEPTION 'eye.raw_evidence: pruning % refused: %', OLD.evidence_id, refusal
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

-- The trigger keeps its name; only its function changes.
DROP TRIGGER raw_evidence_append_only ON eye.raw_evidence;
CREATE TRIGGER raw_evidence_append_only BEFORE UPDATE OR DELETE ON eye.raw_evidence
    FOR EACH ROW EXECUTE FUNCTION eye.guard_raw_evidence();
