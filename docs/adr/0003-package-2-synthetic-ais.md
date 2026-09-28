# ADR 0003: Package 2 synthetic AIS vertical slice

Date: 2026-09-28. Status: proposed (awaiting owner review).

## Decisions

1. **Synthetic AIS adapter on Package 1's contract.** Source `synthetic-ais`
   (vessel layer only) archives invented `synthetic-ais/1` messages unchanged
   inside the capture envelope and maps each to an observation:
   `mmsi` becomes the source record id, `position_time` the observed time and
   `sent_time` the source publication time. EYE receipt time is stamped by
   the adapter, never taken from a message. Vessel ids must be `SYNV-`
   identifiers, so no real MMSI can enter. Low-accuracy reports are stored
   with a flag and excluded from crossing geometry.
2. **Versioned, immutable, synthetic count line.** `reference/lines/
   synthetic-golden-gate.v1.json` is placeholder geometry in the synthetic
   area near 0N 0E, not the real strait. `eye.count_line` stores each
   `(line_id, version)` once with its definition hash; a changed definition
   under the same version is refused. Non-synthetic lines are refused in this
   package.
3. **Algorithm `transit-counter/1`**, a pure function of stored evidence
   (current observation versions and batch coverage). Parameters are part of
   the algorithm version: maximum gap 600 s, maximum speed 25 m/s, side band
   50 m, end margin 250 m, near-line distance 2 km, edge window 3600 s.
   Geometry uses a local equirectangular projection centred on the line
   (adequate at the kilometre scale of one line; not a geodesic claim).
4. **Tracks split at identity, time and quality breaks.** A time gap over
   600 s or a jump implying over 25 m/s splits a vessel's positions. Superseded,
   contested (publication conflict) and low-accuracy positions are excluded.
5. **Crossings are changes of side between consecutive sided positions of
   one track.** Positions within 50 m of the line have no side, so jitter is not
   a crossing. A crossing within 250 m of a line end is ambiguous; beyond the
   ends it is not a crossing of the line. Crossing time and point are linearly
   interpolated and labelled as estimates. A crossing id is derived from the
   algorithm, line version and the two bracketing observations, so the same
   evidence always yields the same id, whatever order it arrived in.
6. **Evidence that cannot rule a crossing in or out is never a zero.** A split,
   track start or track end within 2 km of the line either makes an ambiguous
   crossing (sides differ across it) or insufficient evidence (they do not).
7. **Count states follow Package 1 coverage semantics.**
   - `unknown` with NULL counts: any part of the interval lacks usable
     coverage (missing, unknown or failed; only captures whose requested area
     contains the line count), or there is insufficient track evidence.
   - `partial` with lower-bound counts: ambiguous crossings or partial
     coverage.
   - `qualified` with exact counts: everything else. Only this state can be a
     measured zero.

   The database enforces NULL counts for unknown/failed
   (`transit_count_missing_is_null`) and refuses `qualified` when ambiguous or
   insufficient items exist.
8. **Derived rows are replaced; every run is logged.** Tracks, gaps,
   crossings and counts are replaced for one (source, line, line version,
   algorithm version) scope in one transaction. `eye.derivation_run` keeps an
   append-only record of each run with the fingerprint of the evidence used and
   a summary, so a count changed by late data remains auditable. Re-running on
   unchanged evidence is a no-op (same run id).
9. **Verification re-derives.** `db-replay` now also re-derives every stored
   track, gap, crossing and count from current evidence and reports any
   missing, extra, different or stale row.

## Consequences

- The real Golden Gate line geometry, the NOAA/MarineCadastre historical
  subset and hand-labelled crossings remain the later real-source proof
  (brief section 7, Package 2 row); nothing here downloads or approximates them.
- Counts are hourly bins in the CLI; tests use explicit intervals.
- The derivation reads a source's whole history each run; windowed or
  incremental derivation is left to Package 5 (rollups and retention).
