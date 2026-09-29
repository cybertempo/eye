-- 0003: feed change log for the browser API (Package 3).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 and 0002 are unchanged.
-- * eye.feed_epoch holds one random id per database. A cursor names its epoch,
--   so a cursor from another or a rebuilt database is recognised as expired
--   instead of being silently misread.
-- * eye.feed_change is append-only. One row is written, by trigger, when a
--   capture batch leaves 'pending' (committed or failed) and when a
--   derivation run is recorded. The API turns rows after a client's cursor
--   into sequenced deltas.
-- * Each trigger takes a SHARE ROW EXCLUSIVE lock on eye.feed_change until its
--   transaction ends, so change numbers become visible in commit order: a
--   reader that has seen change n can never later find an uncommitted n-1.
--   Readers (ACCESS SHARE) are not blocked. Expected lock impact: capture
--   commits and derivation runs serialise on this table for the remainder of
--   their transaction; both are infrequent batch operations.
-- * Existing batches and runs are backfilled in their original order.

CREATE TABLE eye.feed_epoch (
    epoch      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    singleton  boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton)
);
INSERT INTO eye.feed_epoch DEFAULT VALUES;
CREATE TRIGGER feed_epoch_append_only BEFORE UPDATE OR DELETE ON eye.feed_epoch
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

CREATE TABLE eye.feed_change (
    change_seq  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('capture_batch', 'derivation_run')),
    batch_id    uuid REFERENCES eye.capture_batch (batch_id),
    run_id      uuid REFERENCES eye.derivation_run (run_id),
    source_id   eye.identifier NOT NULL,
    layer       eye.identifier NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind = 'capture_batch') = (batch_id IS NOT NULL)),
    CHECK ((kind = 'derivation_run') = (run_id IS NOT NULL))
);
CREATE TRIGGER feed_change_append_only BEFORE UPDATE OR DELETE ON eye.feed_change
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

CREATE FUNCTION eye.record_batch_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    LOCK TABLE eye.feed_change IN SHARE ROW EXCLUSIVE MODE;
    INSERT INTO eye.feed_change (kind, batch_id, source_id, layer)
        VALUES ('capture_batch', NEW.batch_id, NEW.source_id, NEW.layer);
    RETURN NULL;
END;
$$;
CREATE TRIGGER capture_batch_feed AFTER UPDATE OF status ON eye.capture_batch
    FOR EACH ROW WHEN (OLD.status = 'pending' AND NEW.status <> 'pending')
    EXECUTE FUNCTION eye.record_batch_change();

CREATE FUNCTION eye.record_run_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    LOCK TABLE eye.feed_change IN SHARE ROW EXCLUSIVE MODE;
    INSERT INTO eye.feed_change (kind, run_id, source_id, layer)
        VALUES ('derivation_run', NEW.run_id, NEW.source_id, 'vessel');
    RETURN NULL;
END;
$$;
CREATE TRIGGER derivation_run_feed AFTER INSERT ON eye.derivation_run
    FOR EACH ROW EXECUTE FUNCTION eye.record_run_change();

INSERT INTO eye.feed_change (kind, batch_id, run_id, source_id, layer, recorded_at)
SELECT kind, batch_id, run_id, source_id, layer, at FROM (
    SELECT 'capture_batch' AS kind, batch_id, NULL::uuid AS run_id, source_id, layer,
           committed_at AS at
    FROM eye.capture_batch WHERE status <> 'pending'
    UNION ALL
    SELECT 'derivation_run', NULL, run_id, source_id, 'vessel', derived_at
    FROM eye.derivation_run
) existing
ORDER BY at, kind, coalesce(batch_id, run_id);
