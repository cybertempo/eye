#!/usr/bin/env python3
"""Generate the invented synthetic AIS capture fixtures for Package 2.

Runs on: developer laptop, CI. Standard library only; no network. Every vessel,
position and time here is invented, in the synthetic test area near 0N 0E.
  scripts/gen_ais_fixtures.py          rewrite tests/fixtures/synthetic/ais/*
  scripts/gen_ais_fixtures.py --check  exit 1 if the committed files are stale
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "synthetic" / "ais"
METRES_PER_DEGREE = 2 * math.pi * 6_371_008.8 / 360
DAY = datetime(2026, 2, 1, tzinfo=UTC)
LINE_LON = 0.3  # reference/lines/synthetic-golden-gate.v1.json
BBOX = [0.1, -0.1, 0.5, 0.1]
SPEED = 8.0  # m/s, about 15.5 knots
STEP = 60  # seconds between reports
SENT_DELAY = 3  # seconds from position to report


def t(hhmmss: str) -> datetime:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return DAY.replace(hour=h, minute=m, second=s)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def east(metres: float) -> float:
    """Longitude offset for metres east of the line near the equator."""
    return metres / METRES_PER_DEGREE


def voyage(vessel, start, waypoints, *, every=STEP, speed=SPEED, skip=()):
    """Sample a constant-speed polyline every `every` seconds from `start`."""
    legs, total = [], 0.0
    for (lon1, lat1), (lon2, lat2) in zip(waypoints, waypoints[1:], strict=False):
        dx = (lon2 - lon1) * METRES_PER_DEGREE * math.cos(math.radians(lat1))
        dy = (lat2 - lat1) * METRES_PER_DEGREE
        length = math.hypot(dx, dy)
        legs.append((total, length, (lon1, lat1), (lon2, lat2)))
        total += length
    out, k = [], 0
    while k * every * speed <= total + 1e-9:
        d = k * every * speed
        for start_d, length, (lon1, lat1), (lon2, lat2) in legs:
            if d <= start_d + length or (start_d, length) == legs[-1][:2]:
                f = 0.0 if length == 0 else min(1.0, (d - start_d) / length)
                lon, lat = lon1 + f * (lon2 - lon1), lat1 + f * (lat2 - lat1)
                break
        when = start + timedelta(seconds=k * every)
        if not any(a <= when < b for a, b in skip):
            out.append(message(vessel, when, lon, lat))
        k += 1
    return out


def message(vessel, when, lon, lat, accuracy="high"):
    return {
        "mmsi": vessel,
        "position_time": stamp(when),
        "sent_time": stamp(when + timedelta(seconds=SENT_DELAY)),
        "lon": round(lon, 7),
        "lat": round(lat, 7),
        "accuracy": accuracy,
    }


def capture(note, start, end, started, finished, status, messages, *, bbox=BBOX):
    attempt = {
        "written_by": "EYE capture adapter; finished_at is the EYE receipt time for every record",
        "started_at": stamp(started),
        "finished_at": stamp(finished),
        "provider_status": status,
        "quota_cost": 0,
    }
    if status == "ok":
        attempt["observed_start"], attempt["observed_end"] = stamp(start), stamp(end)
    return {
        "capture_format": "eye.synthetic-capture/2",
        "synthetic": True,
        "source_id": "synthetic-ais",
        "adapter_version": "synthetic-ais-adapter/1",
        "layer": "vessel",
        "note": note,
        "request": {
            "bbox": bbox,
            "start": stamp(start),
            "end": stamp(end),
            "expected_interval_s": STEP,
        },
        "attempt": attempt,
        "provider_response": {"format": "synthetic-ais/1", "messages": messages},
    }


def hour(note, messages, *, finished="13:00:05", status="ok", bbox=BBOX):
    return capture(
        note, t("12:00:00"), t("13:00:00"), t("13:00:00"), t(finished), status, messages, bbox=bbox
    )


# The reference transit: eastbound (inbound) across the middle of the line.
TRANSIT = voyage("SYNV-0001", t("12:05:00"), [(0.2, 0.0), (0.4, 0.0)])
# A control vessel that never approaches the line.
BYSTANDER = voyage("SYNV-0002", t("12:02:00"), [(0.15, -0.06), (0.15, 0.06)])


def scenarios() -> dict[str, dict[str, dict]]:
    s: dict[str, dict[str, dict]] = {}
    s["transit"] = {
        "01-hour.json": hour(
            "One vessel crosses the line eastbound (inbound); one never approaches it.",
            TRANSIT + BYSTANDER,
        ),
    }
    redelivered = TRANSIT + BYSTANDER + [TRANSIT[20]]
    s["duplicate"] = {
        "01-hour.json": s["transit"]["01-hour.json"],
        "02-redelivery.json": capture(
            "The same reports delivered again later, one of them twice.",
            t("12:00:00"),
            t("13:00:00"),
            t("13:10:00"),
            t("13:10:05"),
            "ok",
            redelivered,
        ),
    }
    s["retry"] = {
        "01-timeout.json": hour(
            "Provider timed out: nothing observed.", [], finished="13:00:30", status="timeout"
        ),
        "02-retry.json": capture(
            "The retried request succeeds with the transit.",
            t("12:00:00"),
            t("13:00:00"),
            t("13:05:00"),
            t("13:05:05"),
            "ok",
            TRANSIT + BYSTANDER,
        ),
    }
    early = [m for m in TRANSIT if not "12:20:00" <= m["position_time"][11:19] < "12:37:00"]
    late = [m for m in TRANSIT if "12:20:00" <= m["position_time"][11:19] < "12:37:00"]
    s["late"] = {
        "01-hour-missing.json": hour(
            "Reports from 12:20 to 12:37 are missing: the vessel "
            "is seen west of the line, then east of it.",
            early,
        ),
        "02-backfill.json": capture(
            "The missing reports arrive late, byte-identical to the originals.",
            t("12:15:00"),
            t("12:40:00"),
            t("13:20:00"),
            t("13:20:02"),
            "ok",
            late,
        ),
    }
    reverse = voyage(
        "SYNV-0003", t("12:05:00"), [(0.24, 0.01), (0.33, 0.01), (0.33, 0.012), (0.24, 0.012)]
    )
    touch = (
        voyage("SYNV-0004", t("12:10:00"), [(0.25, -0.02), (LINE_LON - east(20), -0.02)])
        + [
            message("SYNV-0004", t("12:22:00"), LINE_LON + east(30), -0.02),
            message("SYNV-0004", t("12:23:00"), LINE_LON - east(25), -0.02),
        ]
        + voyage("SYNV-0004", t("12:24:00"), [(LINE_LON - east(480), -0.02), (0.25, -0.02)])
    )
    s["reversal"] = {
        "01-hour.json": hour(
            "SYNV-0003 crosses inbound, turns and crosses outbound. "
            "SYNV-0004 jitters within 50 m of the line and returns west.",
            reverse + touch,
        ),
    }
    s["gap-far"] = {
        "01-hour.json": hour(
            "A 15-minute silence far west of the line, then a normal inbound crossing.",
            voyage(
                "SYNV-0006",
                t("12:00:00"),
                [(0.12, 0.0), (0.4, 0.0)],
                every=45,
                speed=10.0,
                skip=[(t("12:03:00"), t("12:18:00"))],
            ),
        ),
    }
    s["gap-near"] = {
        "01-hour.json": hour(
            "Silent for 16 minutes about 1 km west of the line, reappearing on the same side.",
            voyage(
                "SYNV-0007",
                t("12:05:00"),
                [
                    (0.22, 0.0),
                    (LINE_LON - east(1000), 0.0),
                    (LINE_LON - east(1000), 0.004),
                    (0.18, 0.004),
                ],
                skip=[(t("12:21:00"), t("12:37:00"))],
            ),
        ),
    }
    s["gap-straddle"] = {
        "01-hour.json": hour(
            "Silent for 15 minutes while moving from 1 km west to 1 km east of the line.",
            _straddle(),
        ),
    }
    s["ambiguous"] = {
        "01-hour.json": hour(
            "SYNV-0009 crosses 111 m from the north end (ambiguous); SYNV-0010 crosses mid-line; "
            "SYNV-0011 passes 1.1 km beyond the end (not a crossing).",
            voyage("SYNV-0009", t("12:05:00"), [(0.2, 0.049), (0.4, 0.049)])
            + voyage("SYNV-0010", t("12:06:00"), [(0.2, -0.01), (0.4, -0.01)])
            + voyage("SYNV-0011", t("12:07:00"), [(0.2, 0.06), (0.4, 0.06)]),
        ),
    }
    jump = voyage("SYNV-0012", t("12:05:00"), [(0.2, 0.03), (0.26, 0.03)])
    jump = (
        jump[:10]
        + [message("SYNV-0012", t("12:15:00"), 0.66, 0.03)]
        + [
            message("SYNV-0012", t("12:16:00") + timedelta(minutes=i), 0.25 - east(480 * i), 0.03)
            for i in range(8)
        ]
    )
    s["jump"] = {
        "01-hour.json": hour(
            "SYNV-0012 reports one position 40 km east of its track (an identity or quality "
            "break) and returns; SYNV-0001 crosses normally.",
            jump + TRANSIT,
        ),
    }
    # Turns back 400 m short of the line at 4 m/s; one low-accuracy report puts it
    # 100 m east of the line, at a speed that would otherwise be plausible.
    outlier = voyage(
        "SYNV-0013",
        t("12:05:00"),
        [(0.24, 0.02), (LINE_LON - east(400), 0.02), (0.24, 0.021)],
        speed=4.0,
    )
    outlier.append(message("SYNV-0013", t("12:31:30"), LINE_LON + east(100), 0.02, "low"))
    s["quality"] = {
        "01-hour.json": hour(
            "SYNV-0013 stays west of the line but has one low-accuracy report 100 m east of it; "
            "SYNV-0001 crosses normally.",
            outlier + TRANSIT,
        ),
    }
    first = voyage("SYNV-0014", t("12:00:00"), [(0.25, 0.0), (0.37, 0.0)])
    first = [m for m in first if m["position_time"][11:19] < "12:30:00"]
    s["outage"] = {
        "01-first-half.json": capture(
            "12:00-12:30 observed: SYNV-0014 crosses inbound about 12:11.",
            t("12:00:00"),
            t("12:30:00"),
            t("12:30:00"),
            t("12:30:02"),
            "ok",
            first,
        ),
        "02-second-half-timeout.json": capture(
            "12:30-13:00: the provider timed out.",
            t("12:30:00"),
            t("13:00:00"),
            t("13:00:00"),
            t("13:00:30"),
            "timeout",
            [],
        ),
    }
    s["quiet"] = {
        "01-hour.json": hour(
            "A healthy hour in which no vessel crosses: a measured zero.", BYSTANDER
        ),
    }
    s["wrong-area"] = {
        "01-hour.json": hour(
            "A healthy capture whose requested area does not contain the "
            "line: it says nothing about the line.",
            BYSTANDER,
            bbox=[0.1, -0.1, 0.25, 0.1],
        ),
    }
    # O39: uncertainty windows that span the 13:00 boundary, and an exact control.
    s["boundary"] = {
        "01-three-hours.json": capture(
            "SYNV-0015 is silent from 12:52 to 13:08 while it moves across the line "
            "(ambiguous, either hour). SYNV-0016 crosses definitely between reports at "
            "12:59:50 and 13:00:50 (either hour). SYNV-0017 crosses at 14:30 between reports "
            "a minute apart (exact).",
            t("12:00:00"),
            t("15:00:00"),
            t("15:00:00"),
            t("15:00:05"),
            "ok",
            voyage("SYNV-0015", t("12:36:00"), [(0.22, 0.02), (LINE_LON - east(1000), 0.02)])
            + voyage("SYNV-0015", t("13:08:00"), [(LINE_LON + east(1000), 0.02), (0.38, 0.02)])
            + voyage("SYNV-0016", t("12:36:50"), [(0.2, -0.02), (0.4, -0.02)])
            + voyage("SYNV-0017", t("14:06:49"), [(0.2, -0.03), (0.4, -0.03)]),
        ),
    }
    # O40: a long stay inside the 50 m no-side band, and a short valid passage.
    dwell = voyage("SYNV-0018", t("12:00:00"), [(0.25, 0.01), (LINE_LON - east(100), 0.01)])
    dwell += [
        message("SYNV-0018", t("12:12:00") + timedelta(minutes=i), LINE_LON + east(offset), 0.01)
        for i, offset in enumerate([-30, -10, 10, 30] * 7 + [-20, 20])
    ]
    dwell += voyage("SYNV-0018", t("12:42:00"), [(LINE_LON + east(200), 0.01), (0.35, 0.01)])
    s["band-dwell"] = {
        "01-hour.json": hour(
            "SYNV-0018 stays inside the 50 m no-side band for 30 minutes, then leaves east. "
            "SYNV-0019 passes through the band with one report in it, 120 s between sided "
            "reports.",
            dwell
            + voyage(
                "SYNV-0019",
                t("12:05:00"),
                [(LINE_LON - east(480 * 10 + 20), -0.01), (LINE_LON + east(4800), -0.01)],
            ),
        ),
    }
    # Package 3 demo: four consecutive hours with every count state the DESK shows.
    s["demo"] = {
        "01-exact.json": _demo_hour(
            12,
            "12:00-13:00: SYNV-0020 crosses inbound mid-line; SYNV-0021 never approaches.",
            voyage("SYNV-0020", t("12:05:00"), [(0.2, 0.0), (0.4, 0.0)])
            + voyage("SYNV-0021", t("12:02:00"), [(0.15, -0.06), (0.15, 0.06)]),
        ),
        "02-partial.json": _demo_hour(
            13,
            "13:00-14:00: SYNV-0022 crosses 111 m from the north end (ambiguous); "
            "SYNV-0023 crosses outbound mid-line.",
            voyage("SYNV-0022", t("13:05:00"), [(0.2, 0.049), (0.4, 0.049)])
            + voyage("SYNV-0023", t("13:06:00"), [(0.4, -0.01), (0.2, -0.01)]),
        ),
        "03-outage.json": _demo_hour(14, "14:00-15:00: the provider timed out.", [], "timeout"),
        "04-quiet.json": _demo_hour(
            15,
            "15:00-16:00: a healthy hour in which no vessel crosses (a measured zero).",
            voyage("SYNV-0024", t("15:02:00"), [(0.15, -0.06), (0.15, 0.06)]),
        ),
    }
    return s


def _demo_hour(h, note, messages, status="ok"):
    start = DAY.replace(hour=h)
    end = start + timedelta(hours=1)
    return capture(note, start, end, end, end + timedelta(seconds=5), status, messages)


def _straddle():
    # West at 1 km, silent 15 minutes, then east at 1 km, drifting slowly.
    path = voyage("SYNV-0008", t("12:05:00"), [(0.22, 0.0), (LINE_LON - east(1000), 0.0)])
    last = datetime.fromisoformat(path[-1]["position_time"])
    after = voyage(
        "SYNV-0008", last + timedelta(minutes=15), [(LINE_LON + east(1000), 0.0), (0.38, 0.0)]
    )
    return path + after


def outputs() -> dict[Path, str]:
    files = {}
    for name, captures in scenarios().items():
        for filename, doc in captures.items():
            files[OUT / name / filename] = json.dumps(doc, indent=1) + "\n"
    return files


def main(argv: list[str]) -> int:
    check = "--check" in argv[1:]
    stale = []
    wanted = outputs()
    existing = set(OUT.glob("*/*.json")) if OUT.exists() else set()
    for path in sorted(existing - set(wanted)):
        stale.append(str(path.relative_to(ROOT)))
        if not check:
            path.unlink()
    for path, content in wanted.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        if check:
            stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if check and stale:
        print(f"gen_ais_fixtures: STALE, regenerate: {', '.join(stale)}", file=sys.stderr)
        return 1
    print("gen_ais_fixtures: fixtures are current" if check else f"wrote {len(wanted)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
