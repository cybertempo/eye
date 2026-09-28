-- 0002: versioned count lines, derived vessel tracks, gaps, line crossings and
-- transit counts (Package 2, synthetic AIS vertical slice).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 is unchanged.
-- * Count lines are immutable per (line_id, version); a new geometry is a new
--   version. Every derived row names its line version and algorithm version.
-- * The current tracks, gaps, crossings and counts are derivations: the worker
--   replaces them for one (source, line, line version, algorithm version) scope
--   in a single transaction. Every run is also recorded immutably: its input
--   batch set and evidence fingerprint (eye.derivation_run) and every crossing,
--   gap and count it produced with their evidence links (eye.run_crossing,
--   eye.run_gap, eye.run_count), so both an original and a revised count can be
--   audited and re-derived from exactly the evidence each one used.
-- * A crossing carries its uncertainty window (the two bracketing positions);
--   counts treat a crossing whose window leaves the interval as uncertain.
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
    input_batch_ids   uuid[] NOT NULL,
    -- The intervals this run was asked to count, as [[start, end], ...]. It is
    -- the independent manifest that replay checks count rows against.
    count_intervals   jsonb NOT NULL CHECK (jsonb_typeof(count_intervals) = 'array'),
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
    window_start          timestamptz NOT NULL,
    window_end            timestamptz NOT NULL,
    time_method           text NOT NULL CHECK (time_method = 'linear_interpolation'),
    crossing_point        geometry(Point, 4326) NOT NULL,
    evidence_batch_ids    uuid[] NOT NULL CHECK (cardinality(evidence_batch_ids) >= 1),
    FOREIGN KEY (line_id, line_version) REFERENCES eye.count_line (line_id, version),
    CHECK ((status = 'definite') = (reason IS NULL)),
    CHECK (window_start < window_end AND crossing_time BETWEEN window_start AND window_end)
);
COMMENT ON COLUMN eye.line_crossing.crossing_time IS
    'Estimated (linear interpolation between the bracketing observations), not observed.';
COMMENT ON COLUMN eye.line_crossing.window_start IS
    'Observed time of the last position before the crossing; the crossing lies in [window_start, window_end].';

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
    boundary_crossings  integer NOT NULL CHECK (boundary_crossings >= 0),
    insufficient_gaps   integer NOT NULL CHECK (insufficient_gaps >= 0),
    reason              text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 500),
    coverage_ids        uuid[] NOT NULL,
    crossing_ids        uuid[] NOT NULL,
    insufficient_gap_ids uuid[] NOT NULL,
    FOREIGN KEY (line_id, line_version) REFERENCES eye.count_line (line_id, version),
    CHECK (interval_end > interval_start),
    CONSTRAINT transit_count_missing_is_null CHECK (
        (state IN ('unknown', 'failed') AND inbound IS NULL AND outbound IS NULL
         AND total IS NULL AND reason IS NOT NULL)
        OR (state IN ('qualified', 'partial') AND inbound IS NOT NULL
            AND outbound IS NOT NULL AND total IS NOT NULL
            AND total = inbound + outbound)
    ),
    CONSTRAINT transit_count_partial_has_reason CHECK (state <> 'partial' OR reason IS NOT NULL),
    CONSTRAINT transit_count_qualified_is_certain CHECK (
        state <> 'qualified' OR (ambiguous_crossings = 0 AND boundary_crossings = 0
                                 AND insufficient_gaps = 0)
    ),
    UNIQUE (source_id, line_id, line_version, algorithm_version, interval_start, interval_end)
);

-- Immutable per-run results. Rows are copies, not references to the current
-- tables, so a later run cannot change what an earlier run reported.
CREATE TABLE eye.run_crossing (
    run_id                uuid NOT NULL REFERENCES eye.derivation_run (run_id),
    crossing_id           uuid NOT NULL,
    vessel_id             eye.identifier NOT NULL,
    track_id              uuid,
    before_observation_id uuid NOT NULL REFERENCES eye.observation (observation_id),
    after_observation_id  uuid NOT NULL REFERENCES eye.observation (observation_id),
    direction             text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    status                text NOT NULL CHECK (status IN ('definite', 'ambiguous')),
    reason                text,
    crossing_time         timestamptz NOT NULL,
    window_start          timestamptz NOT NULL,
    window_end            timestamptz NOT NULL,
    crossing_lon          double precision NOT NULL,
    crossing_lat          double precision NOT NULL,
    evidence_batch_ids    uuid[] NOT NULL CHECK (cardinality(evidence_batch_ids) >= 1),
    PRIMARY KEY (run_id, crossing_id),
    CHECK ((status = 'definite') = (reason IS NULL)),
    CHECK (window_start < window_end AND crossing_time BETWEEN window_start AND window_end)
);

CREATE TABLE eye.run_gap (
    run_id                uuid NOT NULL REFERENCES eye.derivation_run (run_id),
    gap_id                uuid NOT NULL,
    vessel_id             eye.identifier NOT NULL,
    before_observation_id uuid REFERENCES eye.observation (observation_id),
    after_observation_id  uuid REFERENCES eye.observation (observation_id),
    gap_start             timestamptz,
    gap_end               timestamptz,
    reason                text NOT NULL CHECK (reason IN ('time_gap', 'implausible_jump',
                                                          'track_start', 'track_end')),
    effect                text NOT NULL CHECK (effect IN ('none', 'ambiguous_crossing',
                                                          'insufficient_evidence')),
    PRIMARY KEY (run_id, gap_id)
);

-- The same rules as eye.transit_count: an immutable record may not hold a
-- zero for an unknown count, a wrong total, or an exact count with uncertainty.
CREATE TABLE eye.run_count (
    run_id               uuid NOT NULL REFERENCES eye.derivation_run (run_id),
    count_id             uuid NOT NULL,
    interval_start       timestamptz NOT NULL,
    interval_end         timestamptz NOT NULL,
    state                eye.coverage_state NOT NULL,
    inbound              integer CHECK (inbound >= 0),
    outbound             integer CHECK (outbound >= 0),
    total                integer CHECK (total >= 0),
    ambiguous_crossings  integer NOT NULL CHECK (ambiguous_crossings >= 0),
    boundary_crossings   integer NOT NULL CHECK (boundary_crossings >= 0),
    insufficient_gaps    integer NOT NULL CHECK (insufficient_gaps >= 0),
    reason               text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 500),
    coverage_ids         uuid[] NOT NULL,
    crossing_ids         uuid[] NOT NULL,
    insufficient_gap_ids uuid[] NOT NULL,
    PRIMARY KEY (run_id, count_id),
    UNIQUE (run_id, interval_start, interval_end),
    CHECK (interval_end > interval_start),
    CONSTRAINT run_count_missing_is_null CHECK (
        (state IN ('unknown', 'failed') AND inbound IS NULL AND outbound IS NULL
         AND total IS NULL AND reason IS NOT NULL)
        OR (state IN ('qualified', 'partial') AND inbound IS NOT NULL
            AND outbound IS NOT NULL AND total IS NOT NULL
            AND total = inbound + outbound)
    ),
    CONSTRAINT run_count_partial_has_reason CHECK (state <> 'partial' OR reason IS NOT NULL),
    CONSTRAINT run_count_qualified_is_certain CHECK (
        state <> 'qualified' OR (ambiguous_crossings = 0 AND boundary_crossings = 0
                                 AND insufficient_gaps = 0)
    )
);

CREATE TRIGGER run_crossing_append_only BEFORE UPDATE OR DELETE ON eye.run_crossing
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
CREATE TRIGGER run_gap_append_only BEFORE UPDATE OR DELETE ON eye.run_gap
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
CREATE TRIGGER run_count_append_only BEFORE UPDATE OR DELETE ON eye.run_count
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
