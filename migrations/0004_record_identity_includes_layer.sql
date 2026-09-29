-- 0004: a source record is identified by (source, layer, record id)
-- (Package 3 review finding O50).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 to 0003 are unchanged.
-- * The observation id now includes the layer, so the same record id reused
--   in two layers (say a flight and a vessel) is two records, never one
--   observation or one version history. Ids from earlier code are not
--   derivable under this rule, so this migration refuses to run on a database
--   that already holds observations: build a new database and ingest its raw
--   evidence again, rather than mixing two id schemes. Expected lock impact:
--   none beyond the view and index replacement on an empty table.
-- * eye.observation_version ranks versions within (source, layer, record,
--   observed time). Its columns are unchanged, with layer added last.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM eye.observation) THEN
        RAISE EXCEPTION 'migration 0004 changes observation identity to include the layer; '
            'this database already holds observations with ids from the earlier rule. '
            'Create a new database and ingest the raw evidence again.'
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END;
$$;

DROP INDEX eye.observation_record_idx;
CREATE INDEX observation_record_idx
    ON eye.observation (source_id, layer, source_record_id, observed_time, source_published_time);

CREATE OR REPLACE VIEW eye.observation_version AS
WITH receipts AS (
    SELECT observation_id, min(received_time) AS first_received_time,
           count(*) AS receipt_count
    FROM eye.observation_receipt GROUP BY observation_id
), ranked AS (
    SELECT o.observation_id, o.source_id, o.source_record_id, o.observed_time,
           o.source_published_time, r.first_received_time, r.receipt_count, o.layer,
           dense_rank() OVER (PARTITION BY o.source_id, o.layer, o.source_record_id,
                                           o.observed_time
                              ORDER BY o.source_published_time) AS version,
           count(*) OVER (PARTITION BY o.source_id, o.layer, o.source_record_id,
                                       o.observed_time, o.source_published_time) AS tier_size,
           max(o.source_published_time) OVER (PARTITION BY o.source_id, o.layer,
                                                o.source_record_id, o.observed_time)
               AS latest_published
    FROM eye.observation o JOIN receipts r USING (observation_id)
)
SELECT v.observation_id, v.source_id, v.source_record_id, v.observed_time,
       v.source_published_time, v.first_received_time, v.receipt_count, v.version,
       CASE WHEN v.tier_size = 1 THEN p.observation_id END AS supersedes_observation_id,
       CASE WHEN v.source_published_time < v.latest_published THEN false
            WHEN v.tier_size = 1 THEN true
            END AS is_current,
       v.tier_size > 1 AS publication_conflict,
       v.layer
FROM ranked v
LEFT JOIN ranked p
       ON p.source_id = v.source_id AND p.layer = v.layer
      AND p.source_record_id = v.source_record_id
      AND p.observed_time = v.observed_time AND p.version = v.version - 1
      AND p.tier_size = 1;
COMMENT ON VIEW eye.observation_version IS
    'Versions of one (source, layer, record) at one observed time. is_current NULL: the latest '
    'publication time holds conflicting versions; unknown, not chosen.';
