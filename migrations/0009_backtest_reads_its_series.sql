-- 0009: a recorded backtest run must read rollups of its own series (Package 5,
-- research slice; review finding on the series parser).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 to 0008 are unchanged. New objects only: one function
--   and one trigger on eye.backtest_run. No existing row is read or rewritten.
-- * Before this, a run for a series that no rollup is derived for (a transit
--   series of a position-only source, an unconfigured count line, a layer the
--   source does not provide) cited no manifest of its own series, so every hour
--   abstained and an empty run could be recorded.
-- * Now every cited manifest must belong to the run's source and layer and be
--   either its coverage (coverage-hourly) or its own series (derivation and
--   scope), and at least one must be its own series. Rollups write manifests
--   only for the combinations they derive, so a run for an unsupported series
--   cannot be recorded.
-- * Expected lock impact: none while migrating (a function and a trigger on a
--   table that only backtests write). At run time, one indexed lookup per
--   recorded run.

CREATE FUNCTION eye.backtest_reads_its_series() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bad text;
BEGIN
    SELECT d.manifest_id::text INTO bad FROM eye.derivation_manifest d
    WHERE d.manifest_id = ANY(NEW.input_manifest_ids)
    AND NOT (
        (d.source_id, d.layer) = (NEW.source_id, NEW.layer)
        AND (
            (d.derivation, d.scope) = (NEW.derivation, NEW.scope)
            OR (d.derivation, d.scope) = ('coverage-hourly', '')
        )
    )
    ORDER BY d.manifest_id
    LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'backtest run % cites manifest %, which is not of its series % % % %',
            NEW.run_id, bad, NEW.source_id, NEW.layer, NEW.derivation, NEW.scope;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM eye.derivation_manifest d
        WHERE d.manifest_id = ANY(NEW.input_manifest_ids)
        AND (d.source_id, d.layer, d.derivation, d.scope)
            = (NEW.source_id, NEW.layer, NEW.derivation, NEW.scope)
    ) THEN
        RAISE EXCEPTION 'backtest run % reads no % % rollup of %:%; no such rollup is '
            'derived, so the series cannot be backtested', NEW.run_id, NEW.derivation,
            NEW.scope, NEW.source_id, NEW.layer;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER backtest_run_reads_its_series BEFORE INSERT ON eye.backtest_run
    FOR EACH ROW EXECUTE FUNCTION eye.backtest_reads_its_series();
