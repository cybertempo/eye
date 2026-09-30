-- 0006: append-only news and media evidence (Package 4d).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 to 0005 are unchanged. Adds tables and a view; no
--   existing row is read or rewritten. Expected lock impact: none on existing
--   tables (new objects only).
-- * A media item is one version of one article, image or video, exactly as its
--   source published it: metadata and a link to the original, never a body,
--   an image or a video. Its id is derived from source, item id and content,
--   so a duplicate delivery adds a receipt, not an item.
-- * Media items are their own evidence, kept apart from event claims: no
--   column here refers to an event case, and nothing here confirms one.
-- * Headlines, bylines, publishers and URLs are untrusted text, bounded and
--   stored as given; the capture parser also refuses control and
--   bidirectional-override characters.
-- * Reuse rights are per item: licensed items name a licence and attribution;
--   link_only and unknown items name neither.
-- * A location states its role (event place, publisher location, or a place
--   merely mentioned), its method (source-stated or automated geocode) and a
--   precision. A capture time for an image or video is the creator's claim.
-- * Corrections, updates and retractions are later versions ordered by the
--   source's revision time (never by load order). Versions revised at the
--   same time with different content conflict: the current one is unknown.
-- * Items and receipts are append-only.

CREATE TABLE eye.media_item (
    media_item_id         uuid PRIMARY KEY,
    source_id             eye.identifier NOT NULL,
    item_id               eye.identifier NOT NULL,
    kind                  text NOT NULL CHECK (kind IN ('article', 'image', 'video')),
    status                text NOT NULL CHECK (status IN ('published', 'updated', 'corrected', 'retracted')),
    first_published_time  timestamptz NOT NULL,
    revision_time         timestamptz NOT NULL,
    url                   text NOT NULL CHECK (length(url) BETWEEN 9 AND 2000 AND url LIKE 'https://%'),
    syndicated_from       text CHECK (syndicated_from IS NULL
                                      OR (length(syndicated_from) BETWEEN 9 AND 2000
                                          AND syndicated_from LIKE 'https://%')),
    publisher             text NOT NULL CHECK (length(publisher) BETWEEN 1 AND 200),
    creator               text CHECK (creator IS NULL OR length(creator) BETWEEN 1 AND 200),
    headline              text CHECK (headline IS NULL OR length(headline) BETWEEN 1 AND 300),
    language              text CHECK (language IS NULL OR language ~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$'),
    rights_status         text NOT NULL CHECK (rights_status IN ('licensed', 'link_only', 'unknown')),
    licence               text CHECK (licence IS NULL OR licence ~ '^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$'),
    attribution           text CHECK (attribution IS NULL OR length(attribution) BETWEEN 1 AND 300),
    capture_time_claimed  timestamptz,
    place_role            text CHECK (place_role IN ('event_place', 'publisher_location', 'mentioned')),
    place_method          text CHECK (place_method IN ('source_stated', 'automated_geocode')),
    place                 geometry(Point, 4326),
    place_precision_m     double precision CHECK (place_precision_m BETWEEN 1 AND 1000000),
    content_sha256        eye.sha256_hex NOT NULL,
    CHECK (revision_time >= first_published_time),
    -- A first publication is its own revision; a later version is revised later.
    CHECK ((status = 'published') = (revision_time = first_published_time)),
    CONSTRAINT licensed_names_licence_and_attribution CHECK (
        (rights_status = 'licensed') = (licence IS NOT NULL)
        AND (rights_status = 'licensed') = (attribution IS NOT NULL)),
    -- Only an image or video has a (claimed) capture time, and it precedes publication.
    CHECK (capture_time_claimed IS NULL
           OR (kind <> 'article' AND capture_time_claimed <= first_published_time)),
    CONSTRAINT place_is_complete CHECK (
        (place IS NULL) = (place_role IS NULL)
        AND (place IS NULL) = (place_method IS NULL)
        AND (place IS NULL) = (place_precision_m IS NULL)),
    CHECK (place IS NULL OR (ST_X(place) BETWEEN -180 AND 180 AND ST_Y(place) BETWEEN -90 AND 90))
);
COMMENT ON COLUMN eye.media_item.revision_time IS
    'When the source issued this version (first publication, update, correction or retraction); orders versions.';
COMMENT ON COLUMN eye.media_item.capture_time_claimed IS
    'When the creator says the image or video was captured. A claim, never verified by EYE.';
COMMENT ON COLUMN eye.media_item.headline IS 'Untrusted text as the source gave it; render only as text.';
CREATE INDEX media_item_version_idx ON eye.media_item (source_id, item_id, revision_time);
CREATE INDEX media_item_published_idx ON eye.media_item (first_published_time);
CREATE INDEX media_item_place_idx ON eye.media_item USING gist (place);

-- Each delivery of a media item, like eye.event_claim_receipt.
CREATE TABLE eye.media_item_receipt (
    media_item_id   uuid NOT NULL REFERENCES eye.media_item (media_item_id),
    batch_id        uuid NOT NULL REFERENCES eye.capture_batch (batch_id),
    evidence_id     uuid NOT NULL,
    received_time   timestamptz NOT NULL,
    adapter_version text NOT NULL,
    PRIMARY KEY (media_item_id, batch_id),
    CONSTRAINT media_receipt_evidence_of_batch FOREIGN KEY (evidence_id, batch_id)
        REFERENCES eye.raw_evidence (evidence_id, batch_id)
);
CREATE INDEX media_item_receipt_batch_idx ON eye.media_item_receipt (batch_id);

CREATE FUNCTION eye.check_media_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    finished timestamptz;
    revised timestamptz;
BEGIN
    SELECT attempt_finished_at INTO finished FROM eye.capture_batch WHERE batch_id = NEW.batch_id;
    SELECT revision_time INTO revised FROM eye.media_item WHERE media_item_id = NEW.media_item_id;
    IF NEW.received_time IS DISTINCT FROM finished THEN
        RAISE EXCEPTION 'eye.media_item_receipt: received_time % is not the batch receipt time %',
            NEW.received_time, finished USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.received_time < revised THEN
        RAISE EXCEPTION 'eye.media_item_receipt: received % before the source revised it %',
            NEW.received_time, revised USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER media_item_receipt_chronology BEFORE INSERT ON eye.media_item_receipt
    FOR EACH ROW EXECUTE FUNCTION eye.check_media_receipt();

CREATE TRIGGER media_item_append_only BEFORE UPDATE OR DELETE ON eye.media_item
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
CREATE TRIGGER media_item_receipt_append_only BEFORE UPDATE OR DELETE ON eye.media_item_receipt
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- Versions of one item (source, item id), ranked by revision time; same rules
-- as eye.event_claim_version. Derived, so it cannot drift.
CREATE VIEW eye.media_item_version AS
WITH receipts AS (
    SELECT media_item_id, min(received_time) AS first_received_time, count(*) AS receipt_count
    FROM eye.media_item_receipt GROUP BY media_item_id
), ranked AS (
    SELECT m.media_item_id, m.source_id, m.item_id, m.revision_time,
           r.first_received_time, r.receipt_count,
           dense_rank() OVER (PARTITION BY m.source_id, m.item_id ORDER BY m.revision_time) AS version,
           count(*) OVER (PARTITION BY m.source_id, m.item_id, m.revision_time) AS tier_size,
           max(m.revision_time) OVER (PARTITION BY m.source_id, m.item_id) AS latest_revision
    FROM eye.media_item m JOIN receipts r USING (media_item_id)
)
SELECT v.media_item_id, v.source_id, v.item_id, v.revision_time,
       v.first_received_time, v.receipt_count, v.version,
       CASE WHEN v.tier_size = 1 THEN p.media_item_id END AS supersedes_media_item_id,
       CASE WHEN v.revision_time < v.latest_revision THEN false
            WHEN v.tier_size = 1 THEN true
            END AS is_current,
       v.tier_size > 1 AS revision_conflict
FROM ranked v
LEFT JOIN ranked p
       ON p.source_id = v.source_id AND p.item_id = v.item_id
      AND p.version = v.version - 1 AND p.tier_size = 1;
COMMENT ON VIEW eye.media_item_version IS
    'is_current NULL: the latest revision time holds conflicting versions; unknown, not chosen.';
