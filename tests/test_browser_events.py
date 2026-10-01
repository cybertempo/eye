"""Package 4c in a real headless browser: world events on THEATRE and DESK.

The browser is started headless inside this module and closed at the end; no
window ever opens. Each negative has a control on the same page or beside it:
review candidates next to sourced reports, a corrected site next to an
uncorrected one, unknown coverage next to measured zero, a refused
inconsistent case next to a valid one.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect
from synthetic_events import DAY, capture, claim, point
from test_browser import (
    WAIT,
    accessibility_problems,
    cells,
    open_app_loose,
    show_desk,
)
from test_events import EVENTS_DEMO

CASE, ASSESSMENT, KIND, REPORTED, TIME, LAST_OBSERVED, EVIDENCE, HISTORY = range(8)


def demo_events(api_server):
    api = api_server()
    for path in sorted(EVENTS_DEMO.glob("*.json")):
        api.ingest(path)
    return api


def event_row(page: Page, case: str):
    return page.locator("#event-facts tbody tr", has=page.locator("th", has_text=f": {case}"))


def open_events(page: Page, api) -> None:
    open_app_loose(page, api)
    show_desk(page)
    expect(page.locator("#event-facts caption")).to_have_text(
        "World events: sourced reports, review candidates and their history", timeout=WAIT
    )


def marks(page: Page) -> dict:
    return json.loads(page.locator("#globe").get_attribute("data-event-marks"))


def test_negatives_read_as_candidates_and_sourced_reports_as_reports(api_server, page):
    api = demo_events(api_server)
    open_events(page, api)
    # Lost signal, AIS gap, slowdown: review candidates that report nothing.
    for case in ("SYN-SL-1", "SYN-GAP-21", "SYN-TS-1"):
        row = event_row(page, case)
        expect(row).to_have_count(1)
        values = cells(row)
        assert values[ASSESSMENT].startswith("Review candidate: "), values
        assert "Not a report of any accident; outcome unknown." in values[ASSESSMENT]
        assert "confirmed" not in values[ASSESSMENT] and "reported (" not in values[ASSESSMENT]
        assert values[EVIDENCE].startswith("none (motion data)")
    # Controls: the sourced reports on the same page.
    expected = {
        "SYN-AV-1": "Aviation accident: confirmed by a final official report",
        "SYN-MC-1": "Marine casualty: preliminary official report (may change)",
        "SYN-RD-2": "Road collision: cleared (official report)",
        "SYN-RD-1": "Road congestion reported (official report); not confirmed by a final report",
    }
    for case, text in expected.items():
        values = cells(event_row(page, case))
        assert values[ASSESSMENT] == text, values
        assert values[CASE].startswith(
            "Synthetic event reports (invented cases; not a real authority)"
        )
        assert values[EVIDENCE].startswith("synthetic-doc:")
    styles = {m["style"] for m in marks(page).values()}
    assert styles == {"report", "review_candidate"}


def test_corrected_site_and_last_observed_position_are_separate(api_server, page):
    api = demo_events(api_server)
    open_events(page, api)
    values = cells(event_row(page, "SYN-MC-1"))
    # The current (corrected) reported area, at its stated precision.
    assert values[REPORTED] == "Area (ring of 4 corners), stated precision ±600 m"
    history = values[HISTORY]
    assert "v1 (superseded)" in history and "v2 (current)" in history
    assert "±1500 m" in history and "±600 m" in history  # both versions kept
    # The track's last observed position, as an observation, in its own column.
    assert "(SYNV-0020)" in values[LAST_OBSERVED]
    assert values[LAST_OBSERVED].endswith("An observation, not the reported site.")
    assert values[LAST_OBSERVED] != values[REPORTED]
    (mc,) = [m for m in marks(page).values() if m["reported"] == ["area"]]
    assert mc["lastObserved"] is True
    # Controls: a report naming no track, and one whose track was never seen.
    assert cells(event_row(page, "SYN-RD-1"))[LAST_OBSERVED].startswith("None: the report names")
    assert cells(event_row(page, "SYN-AV-1"))[LAST_OBSERVED].startswith("Unknown: not linked")
    # A directional road segment says which direction it affects.
    assert "one direction only" in cells(event_row(page, "SYN-RD-2"))[REPORTED]


def test_unknown_event_coverage_is_never_shown_as_none(api_server, page):
    api = demo_events(api_server)
    open_events(page, api)
    coverage = page.locator("#event-coverage").inner_text()
    assert f"{DAY}14:00:00Z to {DAY}16:00:00Z Unknown: no event-report source" in coverage
    assert f"{DAY}12:00:00Z to {DAY}13:00:00Z covered" in coverage


def test_no_event_source_reads_unknown_and_measured_zero_reads_none(api_server, page, tmp_path):
    bare = api_server()
    open_events(page, bare)
    empty = page.locator("#event-facts tbody").inner_text()
    assert REPORTS_UNKNOWN in empty
    assert ZERO_REPORTS not in empty
    # Control: event sources covered the whole view and reported nothing.
    covered = api_server()
    for layer in ("flight", "vessel", "road"):
        covered.ingest(capture(tmp_path, f"zero-{layer}", layer, [], end=DAY + "16:00:00Z"))
    open_events(page, covered)
    zero = page.locator("#event-facts tbody").inner_text()
    # A measured zero of reports, never a claim that no event occurred (O62).
    assert ZERO_REPORTS in zero and OCCURRENCE_UNKNOWN in zero
    assert REPORTS_UNKNOWN not in zero


def test_conflicting_claims_are_unresolved_then_resolved_live(api_server, page, tmp_path):
    api = api_server()
    same = DAY + "12:30:00Z"
    api.ingest(
        capture(
            tmp_path,
            "conflict",
            "vessel",
            [
                claim("CON-9", "vessel_distress", same, point(0.2, 0.0)),
                claim("CON-9", "vessel_distress", same, point(0.3, 0.05)),
            ],
        )
    )
    open_events(page, api)
    row = event_row(page, "CON-9")
    values = cells(row)
    assert values[ASSESSMENT] == (
        f"Unresolved: 2 conflicting claims published at {same}; no current version"
    )
    assert values[REPORTED].count("Point") == 2  # both sites, neither chosen
    (mark,) = marks(page).values()
    assert mark["style"] == "unresolved" and mark["reported"] == ["point", "point"]
    # Control: a later unique claim resolves it, delivered live.
    api.ingest(
        capture(
            tmp_path,
            "resolved",
            "vessel",
            [
                claim(
                    "CON-9", "vessel_distress", DAY + "12:45:00Z", point(0.25, 0.02), status="final"
                )
            ],
            received=DAY + "13:00:20Z",
        )
    )
    api.server.hub.tick()
    expect(row.locator("td").first).to_have_text(
        "Vessel distress: confirmed by a final official report", timeout=WAIT
    )
    (mark,) = marks(page).values()
    assert mark["style"] == "report" and mark["reported"] == ["point"]


def test_inconsistent_case_is_refused_not_shown(api_server, page):
    api = demo_events(api_server)

    def relay(client):
        server = client.connect_to_server()

        def from_server(message):
            if isinstance(message, str) and '"kind":"snapshot"' in message:
                data = json.loads(message)
                for event in data["events"]:
                    if event["case_id"] == "SYN-SL-1":
                        event["standing"] = "report"  # a candidate dressed as a report
                message = json.dumps(data)
            client.send(message)

        server.on_message(from_server)
        client.on_message(lambda message: server.send(message))

    page.route_web_socket("**/api/v0/stream", relay)
    open_events(page, api)
    refused = cells(event_row(page, "SYN-SL-1"))
    assert refused[ASSESSMENT].startswith("Refused an inconsistent case (standing report")
    assert "SYN-SL-1" not in {e.split(":")[0] for e in marks(page)}  # not drawn either
    assert len(marks(page)) == 6
    # Control: the other candidates on the same page are shown as candidates.
    assert cells(event_row(page, "SYN-GAP-21"))[ASSESSMENT].startswith("Review candidate")


def test_event_tables_are_accessible_and_reachable_by_keyboard(api_server, page):
    api = demo_events(api_server)
    open_app_loose(page, api)
    page.get_by_role("tab", name="THEATRE").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#panel-desk")).to_be_visible()
    table = page.locator("#event-facts")
    expect(table.locator("caption")).to_have_count(1, timeout=WAIT)
    assert table.locator("th[scope=col]").count() == 8
    assert table.locator("tbody th[scope=row]").count() == 7
    assert accessibility_problems(page) == []


# --- O56 and O58: event coverage in DESK -------------------------------------------------------

SMALL = (0.0, 0.0, 0.2, 0.2)


REPORTS_UNKNOWN = "Reports are unknown for part of this view"
ZERO_REPORTS = (
    "No event reports in this view: the captures above found none published in their "
    "reporting windows."
)
OCCURRENCE_UNKNOWN = "Whether any event occurred is Unknown"


def reporting_lines(text: str) -> str:
    """The reporting-window lines only, without the occurrence-completeness lines."""
    return "\n".join(line for line in text.splitlines() if "events that occurred" not in line)


def event_coverage_text(page: Page, captures: bool = False) -> str:
    """DESK's event-coverage text; without ``captures``, the batch ids naming
    each row's capture are left out so rows can be compared across databases."""
    text = page.locator("#event-coverage").inner_text()
    return text if captures else re.sub(r" by capture [0-9a-f-]{36}", "", text)


def small_sources(api, tmp_path) -> None:
    """Healthy event sources for every layer, 12:00-16:00, over a small area only."""
    for layer in ("flight", "vessel", "road"):
        api.ingest(
            capture(tmp_path, f"small-{layer}", layer, [], end=DAY + "16:00:00Z", bbox=SMALL)
        )


def test_small_source_area_reads_partial_and_unknown_not_zero(api_server, page, tmp_path):
    api = api_server()
    small_sources(api, tmp_path)
    open_events(page, api)  # the configured view is four times wider than the source area
    text = event_coverage_text(page)
    assert text.count("partly covered (at least 0 cases in this view") == 3
    assert text.count(f"{DAY}12:00:00Z to {DAY}16:00:00Z Unknown: no event-report source") == 3
    assert " covered (0 cases in this view)" not in text
    empty = page.locator("#event-facts tbody").inner_text()
    assert REPORTS_UNKNOWN in empty
    assert ZERO_REPORTS not in empty


def test_view_inside_the_source_area_reads_measured_zero(api_server, page, tmp_path, example_raw):
    """Control: the same sources, with the view wholly inside their area."""
    raw = json.loads(json.dumps(example_raw))
    raw["view"]["bbox"] = [0.05, 0.05, 0.15, 0.15]
    api = api_server(mode_raw=raw)
    small_sources(api, tmp_path)
    open_events(page, api)
    text = event_coverage_text(page)
    assert text.count(f"{DAY}12:00:00Z to {DAY}16:00:00Z covered (0 cases in this view)") == 3
    reports = reporting_lines(text)
    assert "Unknown" not in reports and "partly" not in reports
    assert text.count("completeness Unknown: no approved event source") == 3  # O62
    empty = page.locator("#event-facts tbody").inner_text()
    assert ZERO_REPORTS in empty and OCCURRENCE_UNKNOWN in empty


def test_live_gap_fill_matches_a_fresh_rest_view(api_server, page, tmp_path):
    api = api_server()
    open_events(page, api)
    status = page.locator("#live-status")
    before = event_coverage_text(page)
    assert f"road: reports published {DAY}12:00:00Z to {DAY}16:00:00Z Unknown" in before
    # Control: a capture outside the view arrives as a delta and changes nothing.
    deltas = int(status.get_attribute("data-deltas"))
    api.ingest(capture(tmp_path, "elsewhere", "road", [], bbox=(0.6, 0.6, 0.9, 0.9)))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-deltas", str(deltas + 1), timeout=WAIT)
    assert event_coverage_text(page) == before
    page.reload()
    open_events(page, api)
    assert event_coverage_text(page) == before  # REST agrees
    # A capture filling 12:00-13:00 for the whole view: the gap narrows live.
    snapshots = int(status.get_attribute("data-snapshots"))
    api.ingest(capture(tmp_path, "fills", "road", [], received=DAY + "13:00:20Z"))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-snapshots", str(snapshots + 1), timeout=WAIT)
    expect(page.locator("#event-coverage")).to_contain_text(
        f"reports published {DAY}13:00:00Z to {DAY}16:00:00Z Unknown", timeout=WAIT
    )
    live = event_coverage_text(page)
    assert (
        f"road: reports published {DAY}12:00:00Z to {DAY}13:00:00Z covered (0 cases in this view); "
        f"reports published {DAY}13:00:00Z to {DAY}16:00:00Z Unknown"
    ) in live, live
    # The old gap is gone.
    assert f"road: reports published {DAY}12:00:00Z to {DAY}16:00:00Z Unknown" not in live
    page.reload()
    open_events(page, api)
    assert event_coverage_text(page) == live  # a fresh REST view shows the same


# --- O59: DESK counts are for the view, not the whole capture ---------------------------------


def set_window(page: Page, start: str, hours: int) -> None:
    page.locator("#view-start").fill(start)
    page.locator("#view-hours").fill(str(hours))
    page.get_by_role("button", name="Show interval").click()
    end = f"{start[:11]}{int(start[11:13]) + hours:02d}{start[13:]}"
    expect(page.locator("#view-summary")).to_contain_text(f"{start} to {end}", timeout=WAIT)
    expect(page.locator("#live-status")).to_have_attribute("data-state", "live", timeout=WAIT)


def cover_other_layers(api, tmp_path, end: str = DAY + "13:00:00Z") -> None:
    for layer in ("flight", "vessel"):
        api.ingest(capture(tmp_path, f"quiet-{layer}", layer, [], end=end))


def small_view_api(api_server, example_raw, bbox):
    raw = json.loads(json.dumps(example_raw))
    raw["view"]["bbox"] = list(bbox)
    return api_server(mode_raw=raw)


def test_desk_count_follows_a_smaller_area(api_server, page, tmp_path, example_raw):
    from test_events import three_cases

    api = small_view_api(api_server, example_raw, (0.0, 0.0, 0.2, 0.2))
    api.ingest(three_cases(tmp_path))
    cover_other_layers(api, tmp_path)
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    text = event_coverage_text(page)
    assert f"road: {reports_in('12:00', '13:00')} covered (2 cases in this view)" in text
    assert "3 cases" not in text  # the whole capture's count is never shown for this view
    for case in ("NEAR-EARLY", "NEAR-LATE"):
        expect(event_row(page, case)).to_have_count(1)
    expect(event_row(page, "FAR-EARLY")).to_have_count(0)


def test_desk_area_without_cases_reads_zero_and_an_empty_table(
    api_server, page, tmp_path, example_raw
):
    """Paired: inside the capture area but away from every case."""
    from test_events import three_cases

    api = small_view_api(api_server, example_raw, (0.3, 0.3, 0.45, 0.45))
    api.ingest(three_cases(tmp_path))
    cover_other_layers(api, tmp_path)
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    text = event_coverage_text(page)
    assert f"road: {reports_in('12:00', '13:00')} covered (0 cases in this view)" in text
    assert "Unknown" not in reporting_lines(text)
    empty = page.locator("#event-facts tbody").inner_text()
    assert ZERO_REPORTS in empty and OCCURRENCE_UNKNOWN in empty


def test_desk_count_follows_a_shorter_window(api_server, page, tmp_path):
    api = api_server()
    here = point(0.1, 0.1)
    api.ingest(
        capture(
            tmp_path,
            "two-hours",
            "road",
            [
                claim(
                    "HOUR-12",
                    "road_closure",
                    DAY + "12:16:00Z",
                    here,
                    event_time=DAY + "12:15:00Z",
                    uncertainty=60,
                ),
                claim(
                    "HOUR-13",
                    "road_closure",
                    DAY + "13:16:00Z",
                    here,
                    event_time=DAY + "13:15:00Z",
                    uncertainty=60,
                ),
            ],
            end=DAY + "14:00:00Z",
        )
    )
    cover_other_layers(api, tmp_path, end=DAY + "14:00:00Z")
    open_events(page, api)
    for start, inside, outside in (("12", "HOUR-12", "HOUR-13"), ("13", "HOUR-13", "HOUR-12")):
        set_window(page, f"{DAY}{start}:00:00Z", 1)
        text = event_coverage_text(page)
        # The row is the capture's whole reporting window (O60); only the
        # count follows the shorter view.
        assert f"road: {reports_in('12:00', '14:00')} covered (1 case in this view)" in text, text
        assert "2 cases" not in text
        expect(event_row(page, inside)).to_have_count(1)
        expect(event_row(page, outside)).to_have_count(0)


def test_live_desk_count_matches_a_fresh_rest_view(api_server, page, tmp_path, example_raw):
    from test_events import three_cases

    api = small_view_api(api_server, example_raw, (0.0, 0.0, 0.2, 0.2))
    api.ingest(capture(tmp_path, "cover", "road", []))
    cover_other_layers(api, tmp_path)
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    status = page.locator("#live-status")
    deltas = int(status.get_attribute("data-deltas"))
    api.ingest(three_cases(tmp_path, received=DAY + "13:00:20Z"))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-deltas", str(deltas + 1), timeout=WAIT)
    expect(page.locator("#event-coverage")).to_contain_text("covered (2 cases in this view)")
    live = event_coverage_text(page)
    assert "3 cases" not in live
    expect(event_row(page, "FAR-EARLY")).to_have_count(0)
    page.reload()
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    assert event_coverage_text(page) == live  # a fresh REST view shows the same counts


# --- O60: DESK labels every count with its reporting window -----------------------------------


EVENT_COUNT_NOTE = (
    "Each event-report row is one capture's reporting window (when its reports were published), "
    "not when events happened. A case is counted in the row of every capture that delivered its "
    "latest report: a repeated delivery counts it in more than one row, and a later report or "
    "correction about an earlier event is counted in the later window. Do not add the rows "
    "together; the event table lists each case once."
)


def reports_in(start: str, end: str) -> str:
    return f"reports published {DAY}{start}:00Z to {DAY}{end}:00Z"


def test_desk_later_report_reads_as_its_own_reporting_window(api_server, page, tmp_path):
    from test_events import early_reports, later_reports

    api = api_server()
    api.ingest(early_reports(tmp_path))
    api.ingest(later_reports(tmp_path))
    cover_other_layers(api, tmp_path, end=DAY + "14:00:00Z")
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    text = event_coverage_text(page)
    assert (
        f"road: {reports_in('12:00', '13:00')} covered (1 case in this view); "
        f"{reports_in('13:00', '14:00')} covered (2 cases in this view)"
    ) in text, text
    assert EVENT_COUNT_NOTE in text
    for case in ("EARLY-ONLY", "CORRECTED", "LATE-REPORT"):
        expect(event_row(page, case)).to_have_count(1)
    expect(event_row(page, "LATE-ONLY")).to_have_count(0)
    # The correction is shown with its full history in the table.
    history = cells(event_row(page, "CORRECTED"))[HISTORY]
    assert history.count("published") == 2, history
    # Control: the later hour shows only its ordinary case and one row.
    set_window(page, DAY + "13:00:00Z", 1)
    later = event_coverage_text(page)
    assert f"road: {reports_in('13:00', '14:00')} covered (1 case in this view)" in later, later
    assert reports_in("12:00", "13:00") not in later
    expect(event_row(page, "LATE-ONLY")).to_have_count(1)
    expect(event_row(page, "CORRECTED")).to_have_count(0)


def test_desk_repeated_delivery_is_counted_twice_and_the_table_once(api_server, page, tmp_path):
    from test_events import two_deliveries

    api = api_server()
    first, again = two_deliveries(tmp_path)
    api.ingest(first)
    api.ingest(again)
    cover_other_layers(api, tmp_path)
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    text = event_coverage_text(page)
    assert (
        f"road: {reports_in('12:00', '13:00')} covered (2 cases in this view); "
        f"{reports_in('12:30', '13:30')} covered (1 case in this view)"
    ) in text, text
    assert EVENT_COUNT_NOTE in text
    # Each row names its capture; the table cites the same two for TWICE.
    named = event_coverage_text(page, captures=True)
    batches = api.conn.run(
        "SELECT batch_id::text FROM eye.capture_batch "
        "WHERE layer = 'road' AND source_id = 'synthetic-events'"
    )
    assert len(batches) == 2
    for (batch,) in batches:
        assert f"by capture {batch}" in named
        assert batch in cells(event_row(page, "TWICE"))[EVIDENCE]
    assert page.locator("#event-facts tbody th[scope=row]").count() == 2  # each case once
    expect(event_row(page, "TWICE")).to_have_count(1)
    # Control: a single delivery gives one row equal to the table.
    single = api_server()
    single.ingest(first)
    cover_other_layers(single, tmp_path)
    open_events(page, single)
    set_window(page, DAY + "12:00:00Z", 1)
    alone = event_coverage_text(page)
    assert f"road: {reports_in('12:00', '13:00')} covered (2 cases in this view)" in alone
    assert "12:30:00Z to" not in alone


def test_desk_refuses_an_event_count_without_its_reporting_window(api_server, page, tmp_path):
    from test_events import early_reports

    api = api_server()
    api.ingest(early_reports(tmp_path))
    cover_other_layers(api, tmp_path)

    def relay(client):
        server = client.connect_to_server()

        def from_server(message):
            if isinstance(message, str) and '"kind":"snapshot"' in message:
                data = json.loads(message)
                for row in data["coverage"]:
                    if row["layer"] == "road":
                        row.pop("interval_kind", None)  # read as an occurrence interval
                message = json.dumps(data)
            client.send(message)

        server.on_message(from_server)
        client.on_message(lambda message: server.send(message))

    page.route_web_socket("**/api/v0/stream", relay)
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    text = event_coverage_text(page)
    assert "refused: an event count without its reporting window" in text, text
    assert "road: reports published" not in text
    # Control: the other layers' rows on the same page keep their label.
    assert f"flight: {reports_in('12:00', '13:00')} covered (0 cases in this view)" in text


def test_live_desk_later_report_and_repeat_match_a_fresh_rest_view(api_server, page, tmp_path):
    from test_events import early_reports, later_reports

    api = api_server()
    api.ingest(early_reports(tmp_path))
    cover_other_layers(api, tmp_path, end=DAY + "14:00:00Z")
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    status = page.locator("#live-status")
    assert f"road: {reports_in('12:00', '13:00')} covered (2 cases in this view)" in (
        event_coverage_text(page)
    )
    # A later correction and a later first report about earlier events.
    snapshots = int(status.get_attribute("data-snapshots"))
    api.ingest(later_reports(tmp_path))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-snapshots", str(snapshots + 1), timeout=WAIT)
    expect(page.locator("#event-coverage")).to_contain_text(
        f"{reports_in('13:00', '14:00')} covered (2 cases in this view)", timeout=WAIT
    )
    live = event_coverage_text(page)
    assert f"{reports_in('12:00', '13:00')} covered (1 case in this view)" in live
    page.reload()
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    assert event_coverage_text(page) == live  # a fresh REST view reads the same
    # The same early claims delivered again: a second 12:00-13:00 row.
    snapshots = int(status.get_attribute("data-snapshots"))
    api.ingest(early_reports(tmp_path, "early-again", received=DAY + "13:00:40Z"))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-snapshots", str(snapshots + 1), timeout=WAIT)
    one = f"{reports_in('12:00', '13:00')} covered (1 case in this view)"
    page.wait_for_function(
        "(one) => document.getElementById('event-coverage').innerText.split(one).length === 3",
        arg=one,
        timeout=WAIT,
    )
    repeated = event_coverage_text(page)
    assert f"{one}; {one}; " in repeated, repeated
    page.reload()
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    assert event_coverage_text(page) == repeated


# --- O61: the general coverage table leaves event rows to the labelled panel ------------------

POINTER = (
    "Event-report coverage is not listed here: its intervals are reporting windows and its "
    "per-capture counts must not be added together, so it is shown with those labels under "
    "DESK, world events, Event-report coverage."
)


def general_coverage(page: Page) -> list:
    """The general coverage table (#coverage-facts): caption and body rows."""
    return page.locator("#coverage-facts").evaluate(
        """(t) => [t.caption ? t.caption.textContent : "",
                   [...t.tBodies[0].rows].map((r) => [...r.cells].map((c) => c.textContent))]"""
    )


def track_rows(message: dict) -> list[list[str]]:
    """The non-event rows REST serves, as the general table should show them."""
    rows = [c for c in message["coverage"] if "interval_kind" not in c]
    rows.sort(key=lambda c: (c["interval"]["start"], c["layer"]))
    return [
        [
            f"{c['interval']['start']} to {c['interval']['end']}",
            c["layer"],
            c["state"],
            f"{c['metric']['name']}: "
            + ("Unknown (no data)" if c["metric"]["value"] is None else f"{c['metric']['value']}"),
            c.get("reason", "none"),
        ]
        for c in rows
    ]


def mixed_sources(api, tmp_path) -> None:
    """A later report about an earlier event, one claim delivered twice, and
    ordinary track coverage (the synthetic AIS demo) in the same view."""
    from test_events import early_reports, later_reports, two_deliveries

    api.ingest(early_reports(tmp_path))
    api.ingest(later_reports(tmp_path))
    first, again = two_deliveries(tmp_path)
    api.ingest(first)
    api.ingest(again)


def rest_view(api, start: str = "12", end: str = "13") -> dict:
    west, south, east, north = json.loads(api.get("/api/v0/snapshot")[1])["area"]["bbox"]
    response, body = api.get(
        f"/api/v0/snapshot?bbox={west},{south},{east},{north}"
        f"&start={DAY}{start}:00:00Z&end={DAY}{end}:00:00Z"
    )
    assert response.status == 200, body
    return json.loads(body)


def test_general_coverage_table_leaves_event_rows_to_the_event_panel(api_server, page, tmp_path):
    api = api_server()
    mixed_sources(api, tmp_path)
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    rest = rest_view(api)
    events = [c for c in rest["coverage"] if c["metric"]["name"] == "event_cases_in_view"]
    batches = {c["batch_id"] for c in events if "batch_id" in c}
    assert len(batches) == 4  # early, later, first and again: all serve event rows here
    caption, rows = general_coverage(page)
    assert POINTER in caption
    # Control: ordinary track coverage stays, exactly as REST serves it.
    expected = track_rows(rest)
    assert expected and rows == expected
    table = page.locator("#coverage-facts").text_content()
    assert "event_cases_in_view" not in table and "reports published" not in table
    assert not any(batch in table for batch in batches)
    # The event panel still lists every event row, labelled: the later report's
    # 13:00-14:00 window and both deliveries of one claim.
    text = event_coverage_text(page)
    assert f"{reports_in('13:00', '14:00')} covered (2 cases in this view)" in text, text
    assert f"{reports_in('12:30', '13:30')} covered (1 case in this view)" in text, text
    assert EVENT_COUNT_NOTE in text
    named = event_coverage_text(page, captures=True)
    assert all(f"by capture {batch}" in named for batch in batches)


def test_general_table_without_track_coverage_points_to_the_event_panel(api_server, page, tmp_path):
    """Paired: only event sources in view, so the general table has no row at all."""
    from test_events import early_reports

    api = api_server(ais_files=[])
    api.ingest(early_reports(tmp_path))
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    assert [c for c in rest_view(api)["coverage"] if "interval_kind" not in c] == []
    caption, rows = general_coverage(page)
    assert rows == [[f"No track coverage in view. {POINTER}"]]
    assert "reports published" in event_coverage_text(page)


def test_live_general_coverage_matches_a_reload(api_server, page, tmp_path):
    from synthetic_captures import capture as positions
    from synthetic_captures import record
    from test_events import early_reports, later_reports

    api = api_server()
    api.ingest(early_reports(tmp_path))
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    status = page.locator("#live-status")
    # A later report and correction about earlier events: a fresh snapshot.
    snapshots = int(status.get_attribute("data-snapshots"))
    api.ingest(later_reports(tmp_path))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-snapshots", str(snapshots + 1), timeout=WAIT)
    # The same early claims delivered again: another fresh snapshot.
    snapshots = int(status.get_attribute("data-snapshots"))
    api.ingest(early_reports(tmp_path, "early-again", received=DAY + "13:00:40Z"))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-snapshots", str(snapshots + 1), timeout=WAIT)
    # An ordinary track capture: a delta adding a non-event row.
    deltas = int(status.get_attribute("data-deltas"))
    api.ingest(
        positions(
            tmp_path,
            "flight",
            "flight",
            [record("SYN-FLT-900", DAY + "12:20:00Z", DAY + "12:20:05Z", 0.1, 0.1)],
            start=DAY + "12:00:00Z",
            end=DAY + "13:00:00Z",
            received=DAY + "13:00:50Z",
        )
    )
    api.server.hub.tick()
    expect(status).to_have_attribute("data-deltas", str(deltas + 1), timeout=WAIT)
    live_caption, live_rows = general_coverage(page)
    assert POINTER in live_caption
    assert any(row[1] == "flight" for row in live_rows)  # the new track row is shown
    assert live_rows == track_rows(rest_view(api))
    live_events = event_coverage_text(page, captures=True)
    assert live_events.count(f"{reports_in('12:00', '13:00')} covered (1 case in this view)") == 2
    page.reload()
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    assert general_coverage(page) == [live_caption, live_rows]  # a reload reads the same
    assert event_coverage_text(page, captures=True) == live_events


# --- O62: DESK never reads a reporting window as the absence of events --------------------------


def occurred_in(layer: str, start: str, end: str) -> str:
    return (
        f"{layer}: events that occurred {DAY}{start}:00Z to {DAY}{end}:00Z: completeness "
        "Unknown: no approved event source gives an occurrence-time guarantee"
    )


def test_desk_empty_capture_then_late_first_report_live_and_reload(api_server, page, tmp_path):
    from test_events import empty_hour, late_first_report

    api = api_server()
    api.ingest(empty_hour(tmp_path))
    cover_other_layers(api, tmp_path, end=DAY + "14:00:00Z")
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    before = event_coverage_text(page)
    # A real zero of reports published 12:00-13:00 ...
    assert f"road: {reports_in('12:00', '13:00')} covered (0 cases in this view)" in before
    # ... never read as "no event occurred".
    assert occurred_in("road", "12:00", "13:00") in before, before
    empty = page.locator("#event-facts tbody").inner_text()
    assert ZERO_REPORTS in empty and OCCURRENCE_UNKNOWN in empty
    # The first report of a 12:30 event, published at 13:10, arrives live.
    status = page.locator("#live-status")
    deltas = int(status.get_attribute("data-deltas"))
    api.ingest(late_first_report(tmp_path))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-deltas", str(deltas + 1), timeout=WAIT)
    expect(event_row(page, "LATE-FIRST")).to_have_count(1)
    live = event_coverage_text(page)
    assert (
        f"road: {reports_in('12:00', '13:00')} covered (0 cases in this view); "
        f"{reports_in('13:00', '14:00')} covered (1 case in this view)"
    ) in live, live
    assert occurred_in("road", "12:00", "13:00") in live
    page.reload()
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    assert event_coverage_text(page) == live  # a reload (fresh REST view) reads the same
    expect(event_row(page, "LATE-FIRST")).to_have_count(1)


def test_desk_on_time_report_is_counted_in_the_early_window(api_server, page, tmp_path):
    """Control: what the early capture measures, reports published in its window."""
    api = api_server()
    api.ingest(
        capture(
            tmp_path,
            "on-time",
            "road",
            [
                claim(
                    "ON-TIME",
                    "road_closure",
                    DAY + "12:31:00Z",
                    point(0.1, 0.1),
                    event_time=DAY + "12:30:00Z",
                )
            ],
        )
    )
    cover_other_layers(api, tmp_path)
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    text = event_coverage_text(page)
    assert f"road: {reports_in('12:00', '13:00')} covered (1 case in this view)" in text
    assert occurred_in("road", "12:00", "13:00") in text  # still not a completeness claim
    expect(event_row(page, "ON-TIME")).to_have_count(1)


def test_desk_refuses_an_occurrence_completeness_claim(api_server, page, tmp_path):
    from test_events import empty_hour

    api = api_server()
    api.ingest(empty_hour(tmp_path))
    cover_other_layers(api, tmp_path)

    def relay(client):
        server = client.connect_to_server()

        def from_server(message):
            if isinstance(message, str) and '"kind":"snapshot"' in message:
                data = json.loads(message)
                for row in data["coverage"]:
                    if row["layer"] == "road" and row.get("interval_kind") == "occurrence_window":
                        row["state"] = "qualified"  # "complete", with no guarantee behind it
                        row["metric"]["value"] = 0
                        row.pop("reason", None)
                message = json.dumps(data)
            client.send(message)

        server.on_message(from_server)
        client.on_message(lambda message: server.send(message))

    page.route_web_socket("**/api/v0/stream", relay)
    open_events(page, api)
    set_window(page, DAY + "12:00:00Z", 1)
    # Wait for the requested window's own rows (the control line) before
    # reading: an earlier view's snapshot can still be on the page for a moment.
    expect(page.locator("#event-coverage")).to_contain_text(
        occurred_in("flight", "12:00", "13:00"), timeout=WAIT
    )
    text = event_coverage_text(page)
    assert "refused: a completeness claim without an approved occurrence-time guarantee" in text
    assert occurred_in("road", "12:00", "13:00") not in text
    # Control: the other layers' unaltered rows read unknown.
    assert occurred_in("flight", "12:00", "13:00") in text
