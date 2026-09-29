"""Package 4c in a real headless browser: world events on THEATRE and DESK.

The browser is started headless inside this module and closed at the end; no
window ever opens. Each negative has a control on the same page or beside it:
review candidates next to sourced reports, a corrected site next to an
uncorrected one, unknown coverage next to measured zero, a refused
inconsistent case next to a valid one.
"""

from __future__ import annotations

import json

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
    assert "Events are unknown for part of this view" in empty
    assert "No event cases reported" not in empty
    # Control: event sources covered the whole view and reported nothing.
    covered = api_server()
    for layer in ("flight", "vessel", "road"):
        covered.ingest(capture(tmp_path, f"zero-{layer}", layer, [], end=DAY + "16:00:00Z"))
    open_events(page, covered)
    zero = page.locator("#event-facts tbody").inner_text()
    assert "No event cases reported where event-report sources covered this view." in zero
    assert "unknown" not in zero.lower()


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
