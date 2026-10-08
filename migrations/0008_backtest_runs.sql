-- 0008: recorded backtest runs of coverage-weighted baselines (Package 5,
-- research slice).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 to 0007 are unchanged. New objects only: no existing
--   table, view, function or row is read or rewritten.
-- * A run names the exact rollup manifests it read (input_manifest_ids), so a
--   replay re-reads those manifests, never whatever is current. A late arrival
--   or correction changes a result only through a new manifest and a new run;
--   runs and their results are append-only. A run may cite only manifests
--   that are current and complete when it is recorded; the check locks the
--   batch and manifest tables until commit (O70) and runs only in a READ
--   COMMITTED transaction, so its snapshot is taken after the lock.
-- * Results are deterministic facts, never prose: a verdict, a reason code
--   from a fixed list when it abstains, and numbers. An abstained result has no
--   observed, expected or score value (a gap is never a zero), and an evaluated
--   one needs exposure. The detail object holds integers only.
-- * Expected lock impact: none while migrating; the new tables are created
--   empty. At run time, recording a backtest run holds SHARE locks on
--   eye.capture_batch, eye.derivation_run and eye.derivation_manifest until
--   its transaction ends, so captures, transit runs and rollups wait for it
--   (a backtest is short and bounded).

CREATE TABLE eye.backtest_run (
    run_id             uuid PRIMARY KEY,
    run_seq            bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    algorithm_version  text NOT NULL CHECK (algorithm_version ~ '^baseline-backtest/[0-9]{1,4}$'),
    source_id          eye.identifier NOT NULL,
    layer              eye.identifier NOT NULL,
    derivation         text NOT NULL CHECK (derivation IN ('observations-hourly', 'transit-daily')),
    scope              text NOT NULL CHECK (length(scope) <= 200),
    metric             eye.identifier NOT NULL,
    first_day          date NOT NULL,
    last_day           date NOT NULL,
    params             jsonb NOT NULL CHECK (jsonb_typeof(params) = 'object'),
    input_manifest_ids uuid[] NOT NULL CHECK (cardinality(input_manifest_ids) <= 10000),
    input_sha256       eye.sha256_hex NOT NULL,
    output_row_count   integer NOT NULL CHECK (output_row_count BETWEEN 1 AND 100000),
    output_sha256      eye.sha256_hex NOT NULL,
    detected           integer NOT NULL CHECK (detected >= 0),
    not_detected       integer NOT NULL CHECK (not_detected >= 0),
    abstained          integer NOT NULL CHECK (abstained >= 0),
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (last_day >= first_day AND last_day - first_day < 366),
    CHECK (detected + not_detected + abstained = output_row_count),
    CHECK ((derivation = 'transit-daily') = (scope LIKE 'line:%'))
);
CREATE TRIGGER backtest_run_append_only BEFORE UPDATE OR DELETE ON eye.backtest_run
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- Every cited manifest must exist and still be current and complete when the
-- run is recorded:
-- * no later manifest for its key;
-- * no settled batch of its source and layer overlapping its day that it does
--   not include (a late arrival or correction not yet rolled up);
-- * for a transit-daily manifest, no transit run for the same source, line,
--   line version and algorithm newer than the run it cites (rollups read the
--   newest run, so a newer one, even from the same captures, makes it stale).
-- These are every database input of a rollup: a day's observations and
-- coverage belong to the settled batches whose request overlaps it (capture
-- rejects records outside the request) and are append-only, and a transit
-- run's counts are append-only. Inputs held in code or configuration (the
-- derivation versions, count line definitions and transit algorithm version)
-- are not database state; only the application's full re-derivation
-- (rollups.check) covers them.
-- The function first takes SHARE locks on the batch, transit-run and manifest
-- tables, which last until the transaction ends, so no batch can arrive or
-- settle and no transit run or manifest can be added between this check and
-- the commit (finding O70). The application takes the same locks before its
-- own checks.
--
-- The check is only sound in a READ COMMITTED transaction: there, each
-- statement below takes a new snapshot after the lock is granted, so it sees
-- every batch and manifest committed before the lock. A REPEATABLE READ or
-- SERIALIZABLE transaction keeps the snapshot of its first statement, which a
-- lock cannot refresh, so a correction committed in between would be invisible
-- here. Such a transaction is therefore refused outright.
CREATE FUNCTION eye.backtest_inputs_current() RETURNS trigger
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    bad text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'backtest run % must be recorded in a READ COMMITTED transaction, not %; '
            'an older snapshot could hide a late correction', NEW.run_id,
            current_setting('transaction_isolation');
    END IF;
    LOCK TABLE eye.capture_batch, eye.derivation_run, eye.derivation_manifest IN SHARE MODE;
    SELECT m.id::text INTO bad FROM unnest(NEW.input_manifest_ids) AS m(id)
    WHERE NOT EXISTS (SELECT 1 FROM eye.derivation_manifest d WHERE d.manifest_id = m.id)
    LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'backtest run % cites manifest %, which does not exist', NEW.run_id, bad;
    END IF;
    SELECT d.manifest_id::text INTO bad FROM eye.derivation_manifest d
    WHERE d.manifest_id = ANY(NEW.input_manifest_ids)
    AND (
        EXISTS (
            SELECT 1 FROM eye.derivation_manifest n
            WHERE (n.source_id, n.layer, n.day, n.derivation, n.scope)
                = (d.source_id, d.layer, d.day, d.derivation, d.scope)
            AND n.manifest_seq > d.manifest_seq
        )
        OR EXISTS (
            SELECT 1 FROM eye.capture_batch b
            WHERE b.source_id = d.source_id AND b.layer = d.layer AND b.status <> 'pending'
            AND b.requested_start < d.interval_end AND b.requested_end > d.interval_start
            AND NOT (b.batch_id = ANY(d.input_batch_ids))
        )
        OR EXISTS (
            SELECT 1 FROM eye.derivation_run c
            JOIN eye.derivation_run n
                ON (n.source_id, n.line_id, n.line_version, n.algorithm_version)
                 = (c.source_id, c.line_id, c.line_version, c.algorithm_version)
            WHERE c.run_id = d.transit_run_id
            AND (n.derived_at, n.run_id) > (c.derived_at, c.run_id)
        )
    )
    ORDER BY d.manifest_id
    LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'backtest run % cites manifest %, which is no longer current '
            '(a later manifest, settled batch or transit run supersedes it)', NEW.run_id, bad;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER backtest_run_inputs_current BEFORE INSERT ON eye.backtest_run
    FOR EACH ROW EXECUTE FUNCTION eye.backtest_inputs_current();

CREATE TABLE eye.backtest_result (
    run_id      uuid NOT NULL REFERENCES eye.backtest_run (run_id),
    row_key     text NOT NULL CHECK (row_key ~ '^day:[0-9]{4}-[0-9]{2}-[0-9]{2}:bin:[0-9]{2}$'),
    bin_start   timestamptz NOT NULL,
    bin_end     timestamptz NOT NULL,
    verdict     text NOT NULL CHECK (verdict IN ('detected', 'not_detected', 'abstained')),
    reason_code text CHECK (reason_code IN ('target_not_covered', 'history_insufficient')),
    observed    numeric CHECK (observed >= 0),
    exposure_s  numeric NOT NULL CHECK (exposure_s >= 0),
    expected    numeric CHECK (expected >= 0),
    score       numeric,
    detail      jsonb NOT NULL CHECK (jsonb_typeof(detail) = 'object'),
    PRIMARY KEY (run_id, row_key),
    CHECK (bin_end > bin_start AND bin_end - bin_start <= interval '1 day'),
    CONSTRAINT backtest_abstained_has_reason CHECK ((verdict = 'abstained') = (reason_code IS NOT NULL)),
    CONSTRAINT backtest_gap_is_not_zero CHECK (
        verdict <> 'abstained' OR (observed IS NULL AND expected IS NULL AND score IS NULL)
    ),
    CONSTRAINT backtest_evaluated_has_exposure CHECK (
        verdict = 'abstained'
        OR (observed IS NOT NULL AND expected IS NOT NULL AND score IS NOT NULL AND exposure_s > 0)
    ),
    -- Facts only: every detail value is an integer, never text.
    CONSTRAINT backtest_detail_is_numeric CHECK (
        NOT jsonb_path_exists(detail, '$.* ? (@.type() != "number")')
    )
);
CREATE TRIGGER backtest_result_append_only BEFORE UPDATE OR DELETE ON eye.backtest_result
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
