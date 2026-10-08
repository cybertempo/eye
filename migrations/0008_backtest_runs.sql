-- 0008: recorded backtest runs of coverage-weighted baselines (Package 5,
-- research slice).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 to 0007 are unchanged. New objects only: no existing
--   table, view, function or row is read or rewritten.
-- * A run names the exact rollup manifests it read (input_manifest_ids), so a
--   replay re-reads those manifests, never whatever is current. A late arrival
--   or correction changes a result only through a new manifest and a new run;
--   runs and their results are append-only.
-- * Results are deterministic facts, never prose: a verdict, a reason code
--   from a fixed list when it abstains, and numbers. An abstained result has no
--   observed, expected or score value (a gap is never a zero), and an evaluated
--   one needs exposure. The detail object holds integers only.
-- * Expected lock impact: none on existing tables; the new tables are created
--   empty.

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

-- Every cited manifest must exist (manifests are append-only, so it stays).
CREATE FUNCTION eye.backtest_inputs_exist() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM unnest(NEW.input_manifest_ids) AS m(id)
        WHERE NOT EXISTS (SELECT 1 FROM eye.derivation_manifest d WHERE d.manifest_id = m.id)
    ) THEN
        RAISE EXCEPTION 'backtest run % cites a manifest that does not exist', NEW.run_id;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER backtest_run_inputs_exist BEFORE INSERT ON eye.backtest_run
    FOR EACH ROW EXECUTE FUNCTION eye.backtest_inputs_exist();

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
