-- 0002: versioned count lines, derived vessel tracks, gaps, line crossings and
-- transit counts (Package 2, synthetic AIS vertical slice).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 is unchanged.
-- * Count lines are immutable per (line_id, version); a new geometry is a new
--   version. Every derived row names its line version and algorithm version.
-- * Tracks, gaps, crossings and counts are derivations: the worker replaces
--   them for one (source, line, line version, algorithm version) scope in a
--   single transaction. Every run is logged append-only in eye.derivation_run
--   with the fingerprint of the evidence it used, so earlier results stay
--   auditable after late data changes them.
-- * A transit count in state unknown/failed has NULL counts and a reason;
--   an outage or insufficient track evidence is never a measured zero.

CREATE TABLE eye.count_line (
    line_id           eye.identifier NOT NULL,
    version           integer NOT NULL CHECK (version > 0),
    name              text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    geometry          geometry(LineString, 4326) NOT NULL,
    inbound_side      text NOT NULL CHECK (inbound_side IN ('left', 'right')),
    synthetic         boolean NOT NULL,
    definition_sha256 eye.sha256_hex NOT NULL,
    PRIMARY KEY (line_id, version),
    CHECK (ST_NPoints(geometry) = 2 AND NOT ST_IsEmpty(geometry))
);
CREATE TRIGGER count_line_append_only BEFORE UPDATE OR DELETE ON eye.count_line
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- Append-only log of every derivation run and the evidence it used.
CREATE TABLE eye.derivation_run (
    run_id            uuid PRIMARY KEY,
    kind              text NOT NULL CHECK (kind = 'transit_count'),
    source_id         eye.identifier NOT NULL,
    line_id           eye.identifier NOT NULL,
    line_version      integer NOT NULL,
    algorithm_version text NOT NULL,
    input_fingerprint eye.sha256_hex NOT NULL,
    observation_count integer NOT NULL CHECK (observation_count >= 0),
    coverage_ids      uuid[] NOT NULL,
    summary           jsonb NOT NULL,
    derived_at        timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (line_id, line_version) REFERENCES eye.count_line (line_id, version)
);
CREATE TRIGGER derivation_run_append_only BEFORE UPDATE OR DELETE ON eye.derivation_run
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();

-- Derived: one continuous run of accepted positions for one vessel identity.
CREATE TABLE eye.vessel_track (
    track_id             uuid PRIMARY KEY,
    run_id               uuid NOT NULL REFERENCES eye.derivation_run (run_id),
    source_id            eye.identifier NOT NULL,
    vessel_id            eye.identifier NOT NULL,
    line_id              eye.identifier NOT NULL,
    line_version         integer NOT NULL,
    algorithm_version    text NOT NULL,
    first_observation_id uuid NOT NULL REFERENCES eye.observation (observation_id),
    last_observation_id  uuid NOT NULL REFERENCES eye.observation (observation_id),
    start_time           timestamptz NOT NULL,
    end_time             timestamptz NOT NULL,
    point_count          integer NOT NULL CHECK (point_count >= 1),
    observation_ids      uuid[] NOT NULL,
    CHECK (end_time >= start_time),
    CHECK (cardinality(observation_ids) = point_count)
);

-- Derived: where a vessel's evidence breaks. time_gap / implausible_jump sit
-- between two observations; track_start / track_end mark a vessel that appears
-- or disappears near the line (one observation, open-ended).
CREATE TABLE eye.track_gap (
    gap_id                uuid PRIMARY KEY,
    run_id                uuid NOT NULL REFERENCES eye.derivation_run (run_id),
    source_id             eye.identifier NOT NULL,
    vessel_id             eye.identifier NOT NULL,
    line_id               eye.identifier NOT NULL,
    line_version          integer NOT NULL,
    algorithm_version     text NOT NULL,
    before_observation_id uuid REFERENCES eye.observation (observation_id),
    after_observation_id  uuid REFERENCES eye.observation (observation_id),
    gap_start             timestamptz,
    gap_end               timestamptz,
    reason                text NOT NULL CHECK (reason IN ('time_gap', 'implausible_jump',
                                                          'track_start', 'track_end')),
    effect                text NOT NULL CHECK (effect IN ('none', 'ambiguous_crossing',
                                                          'insufficient_evidence')),
    CHECK (
        (reason IN ('time_gap', 'implausible_jump') AND before_observation_id IS NOT NULL
         AND after_observation_id IS NOT NULL AND gap_end > gap_start)
        OR (reason = 'track_end' AND before_observation_id IS NOT NULL
            AND after_observation_id IS NULL AND gap_start IS NOT NULL AND gap_end IS NULL)
        OR (reason = 'track_start' AND before_observation_id IS NULL
            AND after_observation_id IS NOT NULL AND gap_start IS NULL AND gap_end IS NOT NULL)
    )
);

-- Derived: one crossing of a count line, bracketed by two observations.
CREATE TABLE eye.line_crossing (
    crossing_id           uuid PRIMARY KEY,
    run_id                uuid NOT NULL REFERENCES eye.derivation_run (run_id),
    source_id             eye.identifier NOT NULL,
    vessel_id             eye.identifier NOT NULL,
    line_id               eye.identifier NOT NULL,
    line_version          integer NOT NULL,
    algorithm_version     text NOT NULL,
    track_id              uuid REFERENCES eye.vessel_track (track_id),
    before_observation_id uuid NOT NULL REFERENCES eye.observation (observation_id),
    after_observation_id  uuid NOT NULL REFERENCES eye.observation (observation_id),
    direction             text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    status                text NOT NULL CHECK (status IN ('definite', 'ambiguous')),
    reason                text CHECK (reason IS NULL OR length(reason) <= 200),
    crossing_time         timestamptz NOT NULL,
    time_method           text NOT NULL CHECK (time_method = 'linear_interpolation'),
    crossing_point        geometry(Point, 4326) NOT NULL,
    evidence_batch_ids    uuid[] NOT NULL CHECK (cardinality(evidence_batch_ids) >= 1),
    FOREIGN KEY (line_id, line_version) REFERENCES eye.count_line (line_id, version),
    CHECK ((status = 'definite') = (reason IS NULL))
);
COMMENT ON COLUMN eye.line_crossing.crossing_time IS
    'Estimated (linear interpolation between the bracketing observations), not observed.';

-- Derived: the count for one line, one interval. Coverage state rules apply:
-- unknown/failed -> NULL counts; partial -> lower bound; qualified -> exact.
CREATE TABLE eye.transit_count (
    count_id            uuid PRIMARY KEY,
    run_id              uuid NOT NULL REFERENCES eye.derivation_run (run_id),
    source_id           eye.identifier NOT NULL,
    line_id             eye.identifier NOT NULL,
    line_version        integer NOT NULL,
    algorithm_version   text NOT NULL,
    interval_start      timestamptz NOT NULL,
    interval_end        timestamptz NOT NULL,
    state               eye.coverage_state NOT NULL,
    inbound             integer CHECK (inbound >= 0),
    outbound            integer CHECK (outbound >= 0),
    total               integer CHECK (total >= 0),
    ambiguous_crossings integer NOT NULL CHECK (ambiguous_crossings >= 0),
    insufficient_gaps   integer NOT NULL CHECK (insufficient_gaps >= 0),
    reason              text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 500),
    coverage_ids        uuid[] NOT NULL,
    crossing_ids        uuid[] NOT NULL,
    FOREIGN KEY (line_id, line_version) REFERENCES eye.count_line (line_id, version),
    CHECK (interval_end > interval_start),
    CONSTRAINT transit_count_missing_is_null CHECK (
        (state IN ('unknown', 'failed') AND inbound IS NULL AND outbound IS NULL
         AND total IS NULL AND reason IS NOT NULL)
        OR (state IN ('qualified', 'partial') AND inbound IS NOT NULL
            AND outbound IS NOT NULL AND total = inbound + outbound)
    ),
    CHECK (state <> 'partial' OR reason IS NOT NULL),
    CHECK (state <> 'qualified' OR (ambiguous_crossings = 0 AND insufficient_gaps = 0)),
    UNIQUE (source_id, line_id, line_version, algorithm_version, interval_start, interval_end)
);
