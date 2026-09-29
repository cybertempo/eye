"""THEATRE and DESK in a real headless browser, against a real database.

The browser is started headless inside each test and closed at the end; no
window ever opens. Each check that expects a refusal, an unknown or a gap has
an accepted control next to it.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import AIS_DEMO
from playwright.sync_api import Page, expect, sync_playwright

DEMO_FILES = sorted(AIS_DEMO.glob("*.json"))
WAIT = 15_000


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page
    context.close()
    assert errors == [], errors


def open_app(page: Page, api) -> None:
    page.goto(f"{api.origin}/")
    expect(page.locator("#live-status")).to_have_attribute("data-state", "live", timeout=WAIT)
    expect(page.locator("#transit-counts tbody tr")).to_have_count(4, timeout=WAIT)


def count_row(page: Page, hour: str):
    return page.locator("#transit-counts tbody tr", has_text=f"2026-02-01T{hour}:00:00Z to")


def cells(row) -> list[str]:
    return [c.strip() for c in row.locator("th, td").all_inner_texts()]


def show_desk(page: Page) -> None:
    page.get_by_role("tab", name="DESK").click()
    expect(page.locator("#panel-desk")).to_be_visible()


# --- DESK: unknown, partial and exact counts --------------------------------------------


def test_outage_is_unknown_and_never_zero(api_server, page):
    api = api_server()
    open_app(page, api)
    show_desk(page)
    outage = cells(count_row(page, "14"))
    assert outage[1] == "Unknown: no usable count"
    assert outage[2:5] == ["Unknown (no count)", "Unknown", "Unknown"]
    assert not any(re.search(r"\b0\b", value) for value in outage[2:5])
    # Control: the healthy quiet hour is a measured zero and says so.
    quiet = cells(count_row(page, "15"))
    assert quiet[1:5] == ["Qualified: exact count", "0", "0", "0"]


def test_partial_count_stays_visibly_partial(api_server, page):
    api = api_server()
    open_app(page, api)
    show_desk(page)
    partial = count_row(page, "13")
    values = cells(partial)
    assert values[1] == "Partial: lower bound"
    assert values[2] == "at least 1"
    assert "ambiguous 1" in values[5] and "lower bound" in values[6]
    assert "state-partial" in (partial.get_attribute("class") or "")
    exact = cells(count_row(page, "12"))  # control
    assert exact[1:5] == ["Qualified: exact count", "1", "1", "0"]


def test_desk_shows_evidence_ids_timestamps_and_source_labels(api_server, page):
    api = api_server()
    _, raw = api.get("/api/v0/transits")
    message = json.loads(raw)
    open_app(page, api)
    show_desk(page)
    desk = page.locator("#panel-desk").inner_text()
    for count in message["counts"]:
        for cid in count["coverage_ids"] + count["crossing_ids"]:
            assert cid in desk
    for crossing in message["crossings"]:
        assert crossing["id"] in desk
        assert crossing["before_observation_id"] in desk
        assert all(batch in desk for batch in crossing["evidence_batch_ids"])
        assert f"{crossing['estimated_time']} (estimated by linear interpolation)" in desk
    for coverage in message["coverage"]:
        assert coverage["batch_id"] in desk and coverage["received_time"] in desk
    assert "Synthetic AIS (invented vessels; not a real AIS feed)" in desk
    assert "SYNTHETIC (invented)" in desk
    assert message["run_id"] in desk and "transit-counter/2" in desk


def test_unavailable_counts_show_unknown_not_zero(api_server, page):
    api = api_server()
    error = {
        "schema_version": "eye.wire/2",
        "kind": "error",
        "status": 503,
        "error": "database unavailable; data unknown",
    }
    page.route(
        "**/api/v0/transits*",
        lambda route: route.fulfill(
            status=503, content_type="application/json", body=json.dumps(error)
        ),
    )
    page.goto(f"{api.origin}/")
    show_desk(page)
    summary = page.locator("#transit-summary")
    expect(summary).to_contain_text("Unknown: server said 503", timeout=WAIT)
    assert page.locator("#transit-counts tbody tr").count() == 0
    assert "unknown, not zero" in page.locator("#transit-counts caption").inner_text()


def test_invalid_transits_response_is_refused(api_server, page):
    api = api_server()
    _, raw = api.get("/api/v0/transits")
    message = json.loads(raw)
    outage = next(c for c in message["counts"] if c["state"] == "unknown")
    outage.update(inbound=0, outbound=0, total=0)  # an outage reported as zero
    page.route(
        "**/api/v0/transits*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(message)
        ),
    )
    page.goto(f"{api.origin}/")
    show_desk(page)
    summary = page.locator("#transit-summary")
    expect(summary).to_contain_text("Unknown: invalid response", timeout=WAIT)
    assert page.locator("#transit-counts tbody tr").count() == 0


def test_inconsistent_totals_are_refused(api_server, page):
    api = api_server()
    _, raw = api.get("/api/v0/transits")
    message = json.loads(raw)
    exact = next(c for c in message["counts"] if c["state"] == "qualified" and c["total"] == 1)
    exact["total"] = 2  # schema-valid, but total is not inbound + outbound
    page.route(
        "**/api/v0/transits*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(message)
        ),
    )
    page.goto(f"{api.origin}/")
    show_desk(page)
    expect(page.locator("#transit-summary")).to_contain_text("inconsistent response", timeout=WAIT)


# --- THEATRE: facts independent of rendering ----------------------------------------------


def test_measured_facts_do_not_depend_on_the_rendering_preset(api_server, page):
    api = api_server()
    open_app(page, api)
    canvas = page.locator("#globe")
    facts, pictures = {}, {}
    for preset in ("low", "balanced", "high"):
        page.get_by_label("Rendering preset").select_option(preset)
        expect(canvas).to_have_attribute("data-preset", preset)
        facts[preset] = (
            page.locator("#track-facts").inner_text(),
            page.locator("#coverage-facts").inner_text(),
            # DESK tables; the summary's response time legitimately changes on reload.
            page.locator("#transit-counts").inner_text(),
            page.locator("#transit-crossings").inner_text(),
            page.locator("#transit-coverage").inner_text(),
        )
        pictures[preset] = canvas.screenshot()
    assert facts["low"] == facts["balanced"] == facts["high"]
    assert "SYNV-0020" in facts["low"][0]
    # Control: the presets really do render differently.
    assert len({pictures["low"], pictures["balanced"], pictures["high"]}) == 3


# --- keyboard use ---------------------------------------------------------------------------


def test_tabs_work_from_the_keyboard(api_server, page):
    api = api_server()
    open_app(page, api)
    theatre = page.get_by_role("tab", name="THEATRE")
    theatre.focus()
    page.keyboard.press("ArrowRight")
    desk = page.get_by_role("tab", name="DESK")
    expect(desk).to_be_focused()
    expect(desk).to_have_attribute("aria-selected", "true")
    expect(page.locator("#panel-desk")).to_be_visible()
    expect(page.locator("#panel-theatre")).to_be_hidden()
    page.keyboard.press("Home")
    expect(theatre).to_be_focused()
    expect(page.locator("#panel-theatre")).to_be_visible()
    page.keyboard.press("x")  # control: other keys change nothing
    expect(theatre).to_have_attribute("aria-selected", "true")


def test_globe_rotates_and_zooms_from_the_keyboard(api_server, page):
    api = api_server()
    open_app(page, api)
    globe = page.locator("#globe")
    globe.focus()
    expect(globe).to_be_focused()
    start = globe.get_attribute("aria-label")
    page.keyboard.press("x")  # control: an unmapped key leaves the view alone
    assert globe.get_attribute("aria-label") == start
    page.keyboard.press("ArrowRight")
    moved = globe.get_attribute("aria-label")
    assert moved != start and "centred on" in moved
    zoom = re.search(r"zoom ×(\d+)", moved).group(1)
    page.keyboard.press("+")
    zoomed = int(re.search(r"zoom ×(\d+)", globe.get_attribute("aria-label")).group(1))
    assert abs(zoomed - 2 * int(zoom)) <= 1  # doubled, up to display rounding
    page.keyboard.press("Home")
    assert globe.get_attribute("aria-label") == start


FOCUSED = "document.activeElement.id || document.activeElement.textContent.trim()"


def test_keyboard_reaches_every_control_in_order(api_server, page):
    api = api_server()
    open_app(page, api)
    page.locator("body").focus()
    reached = []
    for _ in range(25):
        page.keyboard.press("Tab")
        reached.append(page.evaluate(FOCUSED))
    for control in (
        "Skip to content",
        "view-start",
        "view-hours",
        "Show interval",
        "tab-theatre",
        "panel-theatre",
        "rotate-west",
        "zoom-in",
        "reset-view",
        "preset",
        "globe",
    ):
        assert control in reached, (control, reached)
    assert reached.index("view-start") < reached.index("tab-theatre") < reached.index("globe")


def test_view_form_reports_errors_accessibly(api_server, page):
    api = api_server()
    open_app(page, api)
    start = page.get_by_label("Start")
    start.fill("yesterday")
    page.get_by_role("button", name="Show interval").click()
    expect(page.get_by_role("alert")).to_contain_text("Start must be a UTC time")
    expect(start).to_have_attribute("aria-invalid", "true")
    expect(start).to_be_focused()
    # Control: a valid interval is accepted and shown.
    start.fill("2026-01-01T00:00:00Z")
    page.get_by_label("Hours").fill("2")
    page.get_by_role("button", name="Show interval").click()
    expect(page.get_by_role("alert")).to_have_text("")
    expect(page.locator("#view-summary")).to_contain_text(
        "2026-01-01T00:00:00Z to 2026-01-01T02:00:00Z", timeout=WAIT
    )
    expect(page.locator("#track-facts")).to_contain_text("SYN-FLT-001", timeout=WAIT)


# --- basic accessibility ---------------------------------------------------------------------


def accessibility_problems(page: Page) -> list[str]:
    """Basic structural checks, run by the same code on the app and on a broken page."""
    problems = []
    if not page.locator("html").get_attribute("lang"):
        problems.append("html has no lang")
    if page.locator("h1").count() != 1:
        problems.append("page needs exactly one h1")
    for field in page.locator("input, select, textarea").all():
        field_id = field.get_attribute("id")
        if not field_id or page.locator(f'label[for="{field_id}"]').count() != 1:
            problems.append(f"unlabelled field {field_id}")
    for button in page.locator("button").all():
        if not button.inner_text().strip() and not button.get_attribute("aria-label"):
            problems.append("button without a name")
    for table in page.locator("table").all():
        if table.locator("caption").count() != 1 or table.locator("th[scope=col]").count() == 0:
            problems.append(f"table {table.get_attribute('id')} lacks a caption or column headers")
    for tab in page.locator('[role="tab"]').all():
        panel = tab.get_attribute("aria-controls")
        if not panel or page.locator(f'#{panel}[role="tabpanel"]').count() != 1:
            problems.append(f"tab {tab.inner_text()} controls no panel")
    for image in page.locator('[role="img"], img, canvas').all():
        if not (image.get_attribute("aria-label") or image.get_attribute("alt")):
            problems.append("image without a text alternative")
    if page.locator('[role="status"], [aria-live]').count() == 0:
        problems.append("no live region for status changes")
    return problems


def test_basic_accessibility_structure(api_server, page):
    api = api_server()
    open_app(page, api)
    show_desk(page)
    assert accessibility_problems(page) == []
    # Control: the same checks catch a page that breaks each rule.
    page.set_content(
        "<html><body><h1>a</h1><h1>b</h1><input id=x><button></button>"
        "<table><tr><td>1</td></tr></table><canvas></canvas>"
        "<div role=tab aria-controls=missing>t</div></body></html>"
    )
    found = accessibility_problems(page)
    assert len(found) == 8, found


def test_state_is_not_conveyed_by_colour_alone(api_server, page):
    api = api_server()
    open_app(page, api)
    show_desk(page)
    for hour, word in (("12", "Qualified"), ("13", "Partial"), ("14", "Unknown")):
        assert cells(count_row(page, hour))[1].startswith(word)


# --- live updates: reconnect and gap recovery -------------------------------------------------


def test_reconnect_resumes_with_a_cursor(api_server, page):
    api = api_server(ais_files=DEMO_FILES[1:])
    open_app_loose(page, api)
    status = page.locator("#live-status")
    first_cursor = page.locator("#cursor").inner_text()
    api.ingest(DEMO_FILES[0])
    for sub in api.server.hub.subscribers():
        sub.abort("test: connection lost")
    expect(status).to_have_attribute("data-reconnects", "1", timeout=WAIT)
    expect(status).to_have_attribute("data-state", "live", timeout=WAIT)
    expect(page.locator("#cursor")).not_to_have_text(first_cursor)
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020", timeout=WAIT)
    assert status.get_attribute("data-gaps") == "0"


def open_app_loose(page: Page, api) -> None:
    page.goto(f"{api.origin}/")
    expect(page.locator("#live-status")).to_have_attribute("data-state", "live", timeout=WAIT)


def test_live_deltas_apply_in_order_without_gaps(api_server, page):
    api = api_server(ais_files=DEMO_FILES[1:])
    open_app_loose(page, api)
    api.ingest(DEMO_FILES[0])
    api.derive()
    api.server.hub.tick()
    status = page.locator("#live-status")
    expect(status).to_have_attribute("data-deltas", "2", timeout=WAIT)
    assert status.get_attribute("data-gaps") == "0"
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020")
    show_desk(page)
    # The new evidence and derivation turn the 12:00 hour from unknown into an exact count.
    expect(count_row(page, "12")).to_contain_text("Qualified: exact count", timeout=WAIT)


def test_a_dropped_delta_is_detected_and_recovered(api_server, page):
    api = api_server(ais_files=DEMO_FILES[1:])
    dropped = []

    def relay(client):
        server = client.connect_to_server()

        def from_server(message):
            if isinstance(message, str) and '"kind":"delta"' in message and not dropped:
                dropped.append(message)  # lose the first delta in transit
                return
            client.send(message)

        server.on_message(from_server)
        client.on_message(lambda message: server.send(message))

    page.route_web_socket("**/api/v0/stream", relay)
    open_app_loose(page, api)
    api.ingest(DEMO_FILES[0])
    api.derive()
    api.server.hub.tick()
    status = page.locator("#live-status")
    expect(status).to_have_attribute("data-gaps", "1", timeout=WAIT)
    expect(status).to_have_attribute("data-state", "live", timeout=WAIT)
    expect(status).to_have_attribute("data-snapshots", "2")
    assert len(dropped) == 1
    # The fresh snapshot carries the change the lost delta described.
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020")


def test_an_invalid_live_message_is_refused(api_server, page):
    api = api_server()

    def relay(client):
        server = client.connect_to_server()
        sent = []

        def from_server(message):
            client.send(message)
            if isinstance(message, str) and '"kind":"snapshot"' in message and not sent:
                sent.append(1)
                bad = json.loads(message)
                bad["kind"] = "delta"  # a delta missing every delta field
                client.send(json.dumps(bad))

        server.on_message(from_server)
        client.on_message(lambda message: server.send(message))

    page.route_web_socket("**/api/v0/stream", relay)
    open_app_loose(page, api)
    status = page.locator("#live-status")
    expect(status).to_have_attribute("data-snapshots", "2", timeout=WAIT)
    expect(status).to_have_attribute("data-state", "live", timeout=WAIT)
    assert status.get_attribute("data-deltas") == "0"
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020")  # control: valid data shown


# --- O45 to O47 ---------------------------------------------------------------------------


def test_change_feed_failure_shows_stale_then_recovers(api_server, admin_url, page):
    from test_api import _cut_database

    api = api_server(ais_files=DEMO_FILES[1:])
    open_app_loose(page, api)
    status, banner = page.locator("#live-status"), page.locator("#stale-banner")
    expect(banner).to_be_hidden()  # control: a healthy feed is not marked stale
    api.server.hub.tick()
    expect(status).to_have_attribute("data-state", "live")

    _cut_database(api, admin_url, allow=False)
    api.server.hub.tick()
    expect(status).to_have_attribute("data-state", "stale", timeout=WAIT)
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("may be out of date")
    assert page.locator("body.stale").count() == 1

    _cut_database(api, admin_url, allow=True)
    from eye.ingest.capture import ingest
    from eye.storage.db import connect

    writer = connect(api.database_url)
    ingest(writer, DEMO_FILES[0].read_bytes())
    writer.close()
    api.server.hub.tick()
    expect(status).to_have_attribute("data-state", "live", timeout=WAIT)
    expect(banner).to_be_hidden()
    expect(status).to_have_attribute("data-snapshots", "2")
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020")


def test_refused_subscription_is_shown_stale_and_gets_no_deltas(api_server, page):
    api = api_server(ais_files=DEMO_FILES[1:])

    def relay(client):
        server = client.connect_to_server()

        def to_server(message):
            doc = json.loads(message)
            if doc.get("kind") == "subscribe":
                doc["interval"] = {"start": "2026-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"}
            server.send(json.dumps(doc))

        client.on_message(to_server)
        server.on_message(lambda message: client.send(message))

    page.route_web_socket("**/api/v0/stream", relay)
    page.goto(f"{api.origin}/")
    status = page.locator("#live-status")
    expect(status).to_have_attribute("data-state", "stale", timeout=WAIT)
    expect(page.locator("#stale-banner")).to_be_visible()
    expect(status).to_contain_text("413")
    (sub,) = api.server.hub.subscribers()
    assert sub.query is None
    api.ingest(DEMO_FILES[0])
    api.server.hub.tick()
    page.wait_for_timeout(500)
    assert status.get_attribute("data-deltas") == "0"


def test_accepted_subscription_is_live_and_not_stale(api_server, page):
    # Control for the refused subscription above: the unmodified request goes live.
    api = api_server(ais_files=DEMO_FILES[1:])
    open_app_loose(page, api)
    expect(page.locator("#stale-banner")).to_be_hidden()
    assert api.server.hub.subscribers()[0].query is not None


def test_track_ids_are_distinct_for_long_similar_records(api_server, page, tmp_path):
    from test_api import _long_record_flights

    api = api_server()
    api.ingest(_long_record_flights(tmp_path))
    open_app(page, api)
    page.get_by_label("Start").fill("2026-01-01T02:00:00Z")
    page.get_by_label("Hours").fill("1")
    page.get_by_role("button", name="Show interval").click()
    rows = page.locator("#track-facts tbody tr")
    expect(rows).to_have_count(2, timeout=WAIT)
    records = sorted(rows.nth(i).locator("th").inner_text() for i in range(2))
    assert records == ["SYN-" + "X" * 122 + "A", "SYN-" + "X" * 122 + "B"]
    ids = [rows.nth(i).locator("td code").first.inner_text() for i in range(2)]
    assert len(set(ids)) == 2 and all(i.startswith("trk-") for i in ids)
    # Control: the ids are the same ones the API gives, so reloads and deltas agree.
    _, raw = api.get("/api/v0/snapshot?start=2026-01-01T02:00:00Z&end=2026-01-01T03:00:00Z")
    assert sorted(ids) == sorted(t["id"] for t in json.loads(raw)["tracks"])


# --- O48: a correction that moves a track out of view -----------------------------------------


def _flight_page(api_server, page, tmp_path):
    from test_api import flight_capture

    api = api_server(ais_files=[])
    api.ingest(
        flight_capture(tmp_path, "original", 0.1, "2026-01-01T03:00:03Z", "2026-01-01T03:05:02Z")
    )
    open_app_loose(page, api)
    expect(page.locator("#track-facts")).to_contain_text("SYN-FLT-900")
    expect(page.locator("#track-facts")).to_contain_text("0.10000°E")
    return api


def test_correction_out_of_view_removes_the_track_from_the_page(api_server, page, tmp_path):
    from test_api import flight_capture

    api = _flight_page(api_server, page, tmp_path)
    api.ingest(
        flight_capture(tmp_path, "moved-out", 0.8, "2026-01-01T03:20:00Z", "2026-01-01T03:20:05Z")
    )
    api.server.hub.tick()
    status = page.locator("#live-status")
    expect(status).to_have_attribute("data-snapshots", "2", timeout=WAIT)
    expect(status).to_have_attribute("data-state", "live")
    expect(page.locator("#track-facts")).not_to_contain_text("SYN-FLT-900")
    assert status.get_attribute("data-deltas") == "0"


def test_correction_inside_the_view_updates_the_track(api_server, page, tmp_path):
    from test_api import flight_capture

    api = _flight_page(api_server, page, tmp_path)
    api.ingest(
        flight_capture(tmp_path, "moved-in", 0.2, "2026-01-01T03:20:00Z", "2026-01-01T03:20:05Z")
    )
    api.server.hub.tick()
    status = page.locator("#live-status")
    expect(status).to_have_attribute("data-deltas", "1", timeout=WAIT)
    expect(page.locator("#track-facts")).to_contain_text("0.20000°E")
    expect(page.locator("#track-facts")).not_to_contain_text("0.10000°E")
    assert status.get_attribute("data-snapshots") == "1"


# --- O49: the stale banner covers connecting and resynchronising --------------------------------


class Relay:
    """A controllable relay between the page and the live server."""

    def __init__(self, *, connect: bool = True) -> None:
        self.connect_now = connect
        self.client = self.server = None
        self.pending: list = []
        self.held: list = []
        self.drop_deltas = 0
        self.hold_resync_snapshots = False
        self.corrupt_resync_snapshots = 0
        self.snapshots = 0
        self.corrupted = 0

    def __call__(self, client) -> None:
        self.client = client
        client.on_message(self._to_server)
        if self.connect_now:
            self.connect()

    def connect(self) -> None:
        self.server = self.client.connect_to_server()
        self.server.on_message(self._from_server)
        for message in self.pending:
            self.server.send(message)
        self.pending.clear()

    def _to_server(self, message) -> None:
        if self.server is None:
            self.pending.append(message)
        else:
            self.server.send(message)

    def _from_server(self, message) -> None:
        kind = json.loads(message).get("kind") if isinstance(message, str) else None
        if kind == "delta" and self.drop_deltas:
            self.drop_deltas -= 1
            return
        if kind == "snapshot":
            self.snapshots += 1
            if self.snapshots > 1 and self.corrupt_resync_snapshots:
                self.corrupt_resync_snapshots -= 1
                self.corrupted += 1
                bad = json.loads(message)
                bad["cursor"] = "not a valid cursor!"
                self.client.send(json.dumps(bad))
                return
            if self.snapshots > 1 and self.hold_resync_snapshots:
                self.held.append(message)
                return
        self.client.send(message)

    def release(self) -> None:
        self.hold_resync_snapshots = False
        for message in self.held:
            self.client.send(message)
        self.held.clear()


def _banner_shown(page, state: str) -> None:
    expect(page.locator("#live-status")).to_have_attribute("data-state", state, timeout=WAIT)
    expect(page.locator("#stale-banner")).to_be_visible()
    assert page.locator("body.stale").count() == 1


def _banner_cleared(page) -> None:
    expect(page.locator("#live-status")).to_have_attribute("data-state", "live", timeout=WAIT)
    expect(page.locator("#stale-banner")).to_be_hidden()
    assert page.locator("body.stale").count() == 0


def test_delayed_connection_shows_the_banner_until_the_snapshot(api_server, page):
    api = api_server()
    relay = Relay(connect=False)
    page.route_web_socket("**/api/v0/stream", relay)
    page.goto(f"{api.origin}/")
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020", timeout=WAIT)  # rendered
    _banner_shown(page, "connecting")
    page.wait_for_timeout(1000)
    _banner_shown(page, "connecting")  # still waiting: still not current
    relay.connect()  # control: once the live snapshot arrives the banner clears
    _banner_cleared(page)


def test_blocked_connection_keeps_the_banner_until_it_is_unblocked(api_server, page):
    from ws_client import WsClient

    api = api_server(api={"max_websockets": 1})
    occupier = WsClient(api.host, api.port)  # holds the only live-socket slot
    assert occupier.status == 101
    page.goto(f"{api.origin}/")
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020", timeout=WAIT)  # rendered
    _banner_shown(page, "reconnecting")  # the server refused the handshake (503)
    page.wait_for_timeout(2500)  # through a retry or two
    _banner_shown(page, "reconnecting")
    assert page.locator("#live-status").get_attribute("data-snapshots") == "0"
    occupier.close()  # control: once a slot is free the retry connects and clears it
    _banner_cleared(page)


def _gap(api, relay) -> None:
    relay.drop_deltas = 1
    api.ingest(DEMO_FILES[0])
    api.derive()
    api.server.hub.tick()


def test_delayed_resync_shows_the_banner_until_the_new_snapshot(api_server, page):
    api = api_server(ais_files=DEMO_FILES[1:])
    relay = Relay()
    page.route_web_socket("**/api/v0/stream", relay)
    page.goto(f"{api.origin}/")
    _banner_cleared(page)  # control: live before the gap
    relay.hold_resync_snapshots = True
    _gap(api, relay)
    _banner_shown(page, "resyncing")
    page.wait_for_timeout(1000)
    _banner_shown(page, "resyncing")
    assert relay.held  # the fresh snapshot is waiting in the relay
    relay.release()
    _banner_cleared(page)
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020")


def test_invalid_resync_snapshot_does_not_clear_the_banner(api_server, page):
    api = api_server(ais_files=DEMO_FILES[1:])
    relay = Relay()
    page.route_web_socket("**/api/v0/stream", relay)
    page.goto(f"{api.origin}/")
    _banner_cleared(page)
    relay.corrupt_resync_snapshots = 1
    relay.hold_resync_snapshots = True  # hold the retry's snapshot after the corrupt one
    _gap(api, relay)
    expect(page.locator("#live-status")).to_have_attribute("data-snapshots", "1")
    for _ in range(50):
        if relay.corrupted and relay.held:
            break
        page.wait_for_timeout(100)
    assert relay.corrupted == 1 and relay.held
    _banner_shown(page, "resyncing")  # a refused snapshot is not a baseline
    assert page.locator("#live-status").get_attribute("data-snapshots") == "1"
    relay.release()  # control: the valid snapshot clears it
    _banner_cleared(page)
    expect(page.locator("#live-status")).to_have_attribute("data-snapshots", "2")


# --- O50 and O51: layered identity; contested positions -----------------------------------------


def _row(page, record: str, kind: str):
    return page.locator("#track-facts tbody tr", has=page.locator("th", has_text=record)).filter(
        has=page.locator("td", has_text=kind)
    )


def test_reused_record_id_shows_one_row_per_layer(api_server, page, tmp_path):
    from test_identity import expected_track_id, same_layer_captures, shared_id_captures

    api = api_server(ais_files=[])
    for path in shared_id_captures(tmp_path) + same_layer_captures(tmp_path):
        api.ingest(path)
    open_app_loose(page, api)
    facts = page.locator("#track-facts")
    expect(facts).to_contain_text("SYN-SHARED-1", timeout=WAIT)
    for kind in ("flight", "vessel"):
        row = _row(page, "SYN-SHARED-1", kind)
        expect(row).to_have_count(1)
        expect(row).to_contain_text(expected_track_id("synthetic-fixture", kind, "SYN-SHARED-1"))
    # Control: the same-layer record is one row with both positions.
    same = page.locator("#track-facts tbody tr", has=page.locator("th", has_text="SYN-SAME-1"))
    expect(same).to_have_count(1)
    assert cells(same)[5] == "2"


def test_contested_last_position_is_shown_as_unresolved_then_resolved(api_server, page, tmp_path):
    from test_identity import conflict_captures, resolution_capture

    api = api_server(ais_files=[])
    for path in conflict_captures(tmp_path):
        api.ingest(path)
    claims = api.conn.run(
        "SELECT observation_id::text FROM eye.observation_version WHERE is_current IS NULL"
    )
    open_app_loose(page, api)
    row = page.locator("#track-facts tbody tr", has=page.locator("th", has_text="SYN-FLT-910"))
    expect(row).to_have_count(1, timeout=WAIT)
    values = cells(row)
    assert values[5] == "1"  # one resolved position
    assert values[8] == "Unresolved: 2 conflicting claims at 2026-01-01T03:05:00Z"
    assert "0.10000°E" not in values[8] and "0.20000°E" not in values[8]
    for (oid,) in claims:
        assert oid in values[9]  # every claim with its evidence
    canvas = page.locator("#globe")
    markers = canvas.get_attribute("data-claim-markers")
    route = int(canvas.get_attribute("data-route-points"))
    assert markers == "2"

    # Control: a later unique correction resolves it, live.
    api.ingest(resolution_capture(tmp_path))
    api.server.hub.tick()
    expect(row).to_contain_text("0.15000°E", timeout=WAIT)
    values = cells(row)
    assert values[5] == "2" and values[9] == "none"
    expect(canvas).to_have_attribute("data-claim-markers", "0")
    assert int(canvas.get_attribute("data-route-points")) == route + 1


# --- O52: routes break at a contested middle time, in every preset ------------------------------

MIDDLE_REC = "SYN-FLT-930"
MIDDLE = "2026-01-01T03:04:00Z"


def _middle_captures(tmp_path, *, resolved: bool):
    """Nine resolved fixes a minute apart around a 03:04 that is contested
    (two claims published together) or, for the control, resolved."""
    from synthetic_captures import capture, record

    window = {"start": "2026-01-01T02:55:00Z", "end": "2026-01-01T03:15:00Z"}
    fixes = [
        record(MIDDLE_REC, f"2026-01-01T03:0{m}:00Z", f"2026-01-01T03:0{m}:03Z", 0.02 * m)
        for m in range(10)
        if m != 4
    ]
    paths = [capture(tmp_path, "route", "flight", fixes, **window, received="2026-01-01T03:15:01Z")]
    claims = [(0.3, 0.2), (-0.3, -0.2)] if not resolved else [(0.08, 0.0)]
    for index, (lon, lat) in enumerate(claims):
        paths.append(
            capture(
                tmp_path,
                f"middle-{index}",
                "flight",
                [record(MIDDLE_REC, MIDDLE, "2026-01-01T03:04:03Z", lon, lat)],
                **window,
                received=f"2026-01-01T03:15:0{2 + index}Z",
            )
        )
    return paths


def _drawn_runs(page, record: str = MIDDLE_REC) -> dict[str, list[list[str]]]:
    """Per preset, the observed times of each run the globe stroked for one record."""
    from test_identity import expected_track_id

    track = expected_track_id("synthetic-fixture", "flight", record)
    canvas = page.locator("#globe")
    drawn = {}
    for preset in ("low", "balanced", "high"):
        page.get_by_label("Rendering preset").select_option(preset)
        expect(canvas).to_have_attribute("data-preset", preset)
        drawn[preset] = json.loads(canvas.get_attribute("data-route-runs"))[track]
    return drawn


def _crosses(run: list[str], moment: str) -> bool:
    return any(a < moment < b for a, b in zip(run, run[1:], strict=False))


def test_route_breaks_at_a_contested_middle_time_in_every_preset(api_server, page, tmp_path):
    api = api_server(ais_files=[])
    for path in _middle_captures(tmp_path, resolved=False):
        api.ingest(path)
    open_app_loose(page, api)
    expect(page.locator("#track-facts")).to_contain_text(MIDDLE_REC, timeout=WAIT)
    drawn = _drawn_runs(page)
    for preset, runs in drawn.items():
        assert len(runs) == 2, (preset, runs)
        before, after = runs
        # The break sits exactly at the contested time; nothing is drawn across it.
        assert before[0] == "2026-01-01T03:00:00Z" and before[-1] == "2026-01-01T03:03:00Z"
        assert after[0] == "2026-01-01T03:05:00Z" and after[-1] == "2026-01-01T03:09:00Z"
        assert not any(_crosses(run, MIDDLE) for run in runs), preset
    # Low really thins the route (stride 4), so the break is not a sampling accident.
    assert sum(map(len, drawn["low"])) < sum(map(len, drawn["high"])) == 9
    assert page.locator("#globe").get_attribute("data-claim-markers") == "2"


def test_resolved_middle_point_draws_one_normal_route(api_server, page, tmp_path):
    """Control: the same route with 03:04 resolved is one unbroken run."""
    api = api_server(ais_files=[])
    for path in _middle_captures(tmp_path, resolved=True):
        api.ingest(path)
    open_app_loose(page, api)
    expect(page.locator("#track-facts")).to_contain_text(MIDDLE_REC, timeout=WAIT)
    drawn = _drawn_runs(page)
    for preset, runs in drawn.items():
        (run,) = runs
        assert run[0] == "2026-01-01T03:00:00Z" and run[-1] == "2026-01-01T03:09:00Z", preset
    assert MIDDLE in drawn["high"][0] and len(drawn["high"][0]) == 10
    assert page.locator("#globe").get_attribute("data-claim-markers") == "0"


# --- O53: another wire version halts instead of looping -----------------------------------------


V1, V2 = '"schema_version":"eye.wire/1"', '"schema_version":"eye.wire/2"'


class VersionRelay:
    """Relays each connection; from ``relabel_from`` on, the 'server' speaks eye.wire/1."""

    def __init__(self, relabel_from: int) -> None:
        self.relabel_from = relabel_from
        self.connections = 0
        self.subscribes: list[int] = []

    def __call__(self, client) -> None:
        self.connections += 1
        number = self.connections
        server = client.connect_to_server()

        def to_server(message):
            if isinstance(message, str) and json.loads(message).get("kind") == "subscribe":
                self.subscribes.append(number)
            server.send(message)

        def from_server(message):
            if number >= self.relabel_from and isinstance(message, str):
                message = message.replace(
                    '"schema_version":"eye.wire/2"', '"schema_version":"eye.wire/1"'
                )
            client.send(message)

        client.on_message(to_server)
        server.on_message(from_server)


def test_reconnect_to_another_wire_version_halts_and_asks_for_a_reload(api_server, page):
    api = api_server()
    relay = VersionRelay(relabel_from=2)
    page.route_web_socket("**/api/v0/stream", relay)
    page.goto(f"{api.origin}/")
    _banner_cleared(page)  # control: the first connection speaks eye.wire/2 and is live
    status = page.locator("#live-status")
    for sub in api.server.hub.subscribers():
        sub.abort("test: connection lost")
    _banner_shown(page, "incompatible")
    expect(status).to_contain_text("eye.wire/1")
    expect(status).to_contain_text("Reload the page")
    page.wait_for_timeout(3000)  # longer than the first reconnect back-off
    # One subscribe on the new connection, no resubscribe loop and no reconnect.
    assert relay.subscribes == [1, 2] and relay.connections == 2
    assert status.get_attribute("data-state") == "incompatible"
    assert status.get_attribute("data-version-mismatches") == "1"
    assert status.get_attribute("data-snapshots") == "1"
    expect(page.locator("#track-facts")).to_contain_text("SYNV-0020")  # kept, marked stale
    # Control: once page and server speak the same version again, a reload is live.
    relay.relabel_from = 10**6
    page.reload()
    _banner_cleared(page)
    assert status.get_attribute("data-version-mismatches") == "0"


# --- O55: times compare chronologically, fractional seconds included ----------------------------

T = "2026-01-01T03:0"
# record: (resolved fix times, contested time, expected runs, expected last position)
ORDER_CASES = {
    # A conflict at 03:04:00Z between 03:03 and a resolved fix at 03:04:00.5Z.
    # As strings "03:04:00Z" sorts after "03:04:00.500000Z".
    "SYN-FLT-951": (
        ["2:00Z", "3:00Z", "4:00.500000Z"],
        "4:00Z",
        [["2:00Z", "3:00Z"], ["4:00.500000Z"]],
        "resolved",
    ),
    # Control: the same shape in whole seconds, where string order is right.
    "SYN-FLT-952": (
        ["2:00Z", "3:00Z", "5:00Z"],
        "4:00Z",
        [["2:00Z", "3:00Z"], ["5:00Z"]],
        "resolved",
    ),
    # Reverse: the conflict at 03:04:00.5Z is the latest time, after a fix at 03:04:00Z.
    "SYN-FLT-953": (
        ["2:00Z", "3:00Z", "4:00Z"],
        "4:00.500000Z",
        [["2:00Z", "3:00Z", "4:00Z"]],
        "unresolved",
    ),
    # Control: the conflict latest in whole seconds.
    "SYN-FLT-954": (
        ["2:00Z", "3:00Z", "4:00Z"],
        "5:00Z",
        [["2:00Z", "3:00Z", "4:00Z"]],
        "unresolved",
    ),
}


def _order_captures(tmp_path):
    from synthetic_captures import capture, record

    window = {"start": "2026-01-01T02:55:00Z", "end": "2026-01-01T03:15:00Z"}
    paths = []
    for n, (rec, (fixes, contested, _, _)) in enumerate(ORDER_CASES.items()):
        lat = 0.1 * n - 0.15
        route = [record(rec, T + f, T + f, 0.02 * i, lat) for i, f in enumerate(fixes)]
        paths.append(
            capture(
                tmp_path, f"route-{rec}", "flight", route, **window, received="2026-01-01T03:15:01Z"
            )
        )
        for side, lon in enumerate((0.3, -0.3)):
            paths.append(
                capture(
                    tmp_path,
                    f"claim-{rec}-{side}",
                    "flight",
                    [record(rec, T + contested, "2026-01-01T03:10:00Z", lon, lat)],
                    **window,
                    received=f"2026-01-01T03:15:0{2 + side}Z",
                )
            )
    return paths


@pytest.mark.parametrize("rec", sorted(ORDER_CASES))
def test_fractional_seconds_order_routes_and_last_position(api_server, page, tmp_path, rec):
    fixes, contested, runs, last = ORDER_CASES[rec]
    api = api_server(ais_files=[])
    for path in _order_captures(tmp_path):
        api.ingest(path)
    open_app_loose(page, api)
    row = page.locator("#track-facts tbody tr", has=page.locator("th", has_text=rec))
    expect(row).to_have_count(1, timeout=WAIT)
    expected = [[T + f for f in run] for run in runs]
    problems = []  # collected, so a failure reports the route and DESK text together
    drawn = _drawn_runs(page, rec)
    if drawn["high"] != expected:  # every resolved fix, split where expected
        problems.append(("high runs", drawn["high"]))
    for preset, runs_drawn in drawn.items():
        # Thinning keeps each run's ends and never moves a break.
        ends = [(r[0], r[-1]) for r in runs_drawn]
        if ends != [(r[0], r[-1]) for r in expected]:
            problems.append((f"{preset} run ends", ends))
        elif any(not set(r) <= set(f) for r, f in zip(runs_drawn, expected, strict=True)):
            problems.append((f"{preset} run points", runs_drawn))
    position = cells(row)[8]
    if last == "unresolved":
        wanted = f"Unresolved: 2 conflicting claims at {T + contested}"
        if position != wanted:
            problems.append(("DESK last position", position))
    else:
        lon = 0.02 * (len(fixes) - 1)
        if position.startswith("Unresolved") or f"{lon:.5f}°E" not in position:
            problems.append(("DESK last position", position))
    assert problems == []
