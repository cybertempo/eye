"""Package 4d in a real headless browser: news and media evidence on DESK and THEATRE.

The browser is started headless inside this module and closed at the end; no
window ever opens. Each negative has a control on the same page or beside it:
a withheld headline beside a licensed image's, a publisher's city and an
automated geocode beside a drawn stated place, old footage beside a current
image, a refused item beside valid ones, unknown news coverage beside a
measured zero of items.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect
from synthetic_media import DAY, capture, item, place
from test_browser import WAIT, accessibility_problems, cells, open_app_loose, show_desk
from test_browser_events import set_window
from test_media import MEDIA_DEMO

(
    ITEM,
    SHOWS,
    PUBLISHER,
    HEADLINE,
    TIMES,
    PLACE,
    RIGHTS,
    ORIGINAL,
    HISTORY,
) = range(9)
CAPTION = "News and media evidence: what each source published, with its rights and history"


def demo(api_server):
    api = api_server()
    for path in sorted(MEDIA_DEMO.glob("*.json")):
        api.ingest(path)
    return api


def row(page: Page, item_id: str):
    return page.locator("#media-facts tbody tr", has=page.locator("th", has_text=f": {item_id}"))


def open_media(page: Page, api, hours: int = 4) -> None:
    open_app_loose(page, api)
    show_desk(page)
    set_window(page, DAY + "12:00:00Z", hours)
    expect(page.locator("#media-facts caption")).to_have_text(CAPTION, timeout=WAIT)


def media_marks(page: Page) -> dict:
    return json.loads(page.locator("#globe").get_attribute("data-media-marks") or "{}")


def desk_media(page: Page) -> tuple[str, str]:
    return page.locator("#media-facts").inner_text(), page.locator("#media-coverage").inner_text()


def names(page: Page) -> dict:
    """Item id -> wire id, read from the page's own API view."""
    view = page.evaluate(
        "async () => (await (await fetch('/api/v0/snapshot?start=2026-02-01T12:00:00Z"
        "&end=2026-02-01T16:00:00Z')).json()).media"
    )
    return {m["item_id"]: m["id"] for m in view}


def test_items_show_source_times_place_rights_and_history(api_server, page):
    api = demo(api_server)
    open_media(page, api)
    a1 = cells(row(page, "SN-A1"))
    assert a1[ITEM] == "Synthetic news and media (invented items; not a real publisher): SN-A1"
    assert "Evidence that Synthetic Daily published it; not a verified account" in a1[SHOWS]
    assert a1[PUBLISHER] == "Synthetic Daily; by Invented Reporter"
    assert "First published 2026-02-01T12:10:00Z (source time)" in a1[TIMES]
    assert "This version: corrected 2026-02-01T13:15:00Z (source time)" in a1[TIMES]
    assert "Received by EYE 2026-02-01T14:00:10Z" in a1[TIMES]
    assert "Age at the end of this view: 3 h 50 min" in a1[TIMES]
    assert a1[PLACE].startswith("Event place stated by the source")
    assert "± 500 m. Drawn as an approximate area, not a site." in a1[PLACE]
    assert a1[RIGHTS].startswith("Link and metadata only") and "no copy" in a1[RIGHTS]
    assert a1[ORIGINAL] == "https://synthetic-daily.invalid/bridge-closure"
    assert a1[HISTORY].count("received") == 2 and "superseded" in a1[HISTORY]
    link = row(page, "SN-A1").locator("a")
    assert link.get_attribute("rel") == "noopener noreferrer nofollow"
    assert link.get_attribute("target") is None  # nothing opens by itself
    # A late delivery shows its delay; the publication time stays the source's.
    l1 = cells(row(page, "SN-L1"))
    assert "First published 2026-02-01T13:20:00Z" in l1[TIMES]
    assert "Received by EYE 2026-02-01T15:30:00Z (2 h 10 min after this version)" in l1[TIMES]


def test_unknown_rights_withhold_the_headline_beside_a_licensed_control(api_server, page):
    api = demo(api_server)
    open_media(page, api)
    v1 = cells(row(page, "SN-V1"))
    assert v1[HEADLINE] == "Headline withheld: reuse rights unknown."
    assert v1[RIGHTS].startswith("Reuse rights unknown: link only")
    assert v1[ORIGINAL] == "https://clips.invalid/watch/abc123"  # the link stays
    assert "flooding" not in page.locator("#panel-desk").inner_text()
    # Control: a licensed image shows its headline, licence and attribution.
    p1 = cells(row(page, "SN-P1"))
    assert p1[HEADLINE] == "Invented: closed bridge seen from the east bank"
    assert p1[RIGHTS].startswith(
        "Licensed CC-BY-4.0; attribution: Invented Photographer / Invented Commons, CC BY 4.0."
    )


def test_old_footage_wrong_city_and_geocodes_are_not_marks(api_server, page):
    api = demo(api_server)
    open_media(page, api)
    v1 = cells(row(page, "SN-V1"))
    assert (
        "Captured 2024-05-01T09:00:00Z according to the creator (a claim, not verified)"
        in (v1[TIMES])
    )
    assert "Not live." in v1[TIMES]
    assert "It does not show when or where it was captured, or that it is live." in v1[SHOWS]
    assert "Not drawn: captured outside this view, according to its creator." in v1[PLACE]
    w1 = cells(row(page, "SN-W1"))[PLACE]
    assert w1.startswith("Publisher's location") and "Not drawn: the publisher's location" in w1
    g1 = cells(row(page, "SN-G1"))[PLACE]
    assert "automated geocode" in g1 and "Not drawn" in g1
    ids = names(page)
    drawn = media_marks(page)
    for negative in ("SN-V1", "SN-W1", "SN-G1", "SN-C1"):
        assert ids[negative] not in drawn, negative
    # Controls: a stated event place and a current licensed image are drawn,
    # as approximate areas at their stated precision.
    assert drawn[ids["SN-A1"]] == {"style": "approximate_area", "precision_m": 500}
    assert drawn[ids["SN-P1"]] == {"style": "approximate_area", "precision_m": 500}


def test_suggestions_are_labelled_and_items_stay_separate(api_server, page):
    api = demo(api_server)
    open_media(page, api)
    text = page.locator("#media-suggestions").inner_text()
    label = "Synthetic news and media (invented items; not a real publisher)"
    assert (
        f"Suggestion, not confirmed: {label}: SN-A1 and {label}: SN-C1 are copies of one report "
        "(syndication). They are not independent confirmation."
    ) in text
    assert (
        f"Suggestion, not confirmed: {label}: SN-A1 and {label}: SN-B1 come from different "
        "publishers"
    ) in text
    assert "confirmed:" not in text.replace("not confirmed:", "")
    for item_id in ("SN-A1", "SN-B1", "SN-C1"):
        expect(row(page, item_id)).to_have_count(1)  # never merged
    c1 = cells(row(page, "SN-C1"))[ORIGINAL]
    assert "Syndicated from https://synthetic-daily.invalid/bridge-closure: a copy" in c1
    # News never becomes a world event.
    assert page.locator("#event-facts tbody th[scope=row]").count() == 0


def test_headline_markup_is_only_text(api_server, page, tmp_path):
    api = api_server()
    markup = '<img src=x onerror="window.__pwned = 1"> Invented & <b>bold</b>'
    api.ingest(
        capture(
            tmp_path,
            "markup",
            [
                item("MARKUP", DAY + "12:10:00Z", headline=markup),
                item("PLAIN", DAY + "12:20:00Z", headline="Invented plain headline"),
            ],
        )
    )
    open_media(page, api)
    assert cells(row(page, "MARKUP"))[HEADLINE] == markup
    assert cells(row(page, "PLAIN"))[HEADLINE] == "Invented plain headline"  # control
    assert page.locator("#media-facts img, #media-facts b").count() == 0
    assert page.evaluate("window.__pwned") is None


def test_an_inconsistent_item_is_refused_not_shown(api_server, page):
    api = demo(api_server)

    def relay(client):
        server = client.connect_to_server()

        def from_server(message):
            if isinstance(message, str) and '"kind":"snapshot"' in message:
                data = json.loads(message)
                for served in data["media"]:
                    if served["item_id"] == "SN-V1":
                        version = served["versions"][0]
                        version["headline"] = "Leaked headline"  # with unknown rights
                        version["headline_withheld"] = False
                message = json.dumps(data)
            client.send(message)

        server.on_message(from_server)
        client.on_message(lambda message: server.send(message))

    page.route_web_socket("**/api/v0/stream", relay)
    open_media(page, api)
    refused = cells(row(page, "SN-V1"))
    assert refused[SHOWS].startswith(
        "Refused an inconsistent item (version 1 shows a headline with unknown rights)"
    )
    assert "Leaked headline" not in page.locator("#panel-desk").inner_text()
    # Control: the other items on the same page are shown.
    assert cells(row(page, "SN-P1"))[HEADLINE].startswith("Invented: closed bridge")


def test_no_news_source_is_unknown_and_an_empty_capture_a_zero(api_server, page, tmp_path):
    bare = api_server()
    open_media(page, bare)
    coverage = page.locator("#media-coverage").inner_text()
    assert "Unknown: no news source covers this area and time" in coverage
    empty = page.locator("#media-facts tbody").inner_text()
    assert "News is unknown for part of this view" in empty
    # Control: a healthy source covering the whole view published nothing.
    covered = api_server()
    covered.ingest(capture(tmp_path, "empty", [], end=DAY + "16:00:00Z"))
    open_media(page, covered)
    zero = page.locator("#media-facts tbody").inner_text()
    assert "the covered sources published none in their reporting windows" in zero
    assert "That does not mean nothing happened." in zero
    assert "covered (0 items in this view)" in page.locator("#media-coverage").inner_text()


def test_outage_reads_failed_never_no_news(api_server, page, tmp_path):
    api = api_server()
    api.ingest(capture(tmp_path, "down", [], end=DAY + "16:00:00Z", status="timeout"))
    open_media(page, api)
    coverage = page.locator("#media-coverage").inner_text()
    assert "Unknown: provider status timeout" in coverage
    assert "0 items" not in coverage
    empty = page.locator("#media-facts tbody").inner_text()
    assert "News is unknown for part of this view" in empty


def test_live_item_and_repeat_match_a_reload(api_server, page, tmp_path):
    api = api_server()
    api.ingest(capture(tmp_path, "cover", [], end=DAY + "16:00:00Z"))
    open_media(page, api)
    status = page.locator("#live-status")
    deltas = int(status.get_attribute("data-deltas"))
    lone = item("LIVE-1", DAY + "12:20:00Z", where=place(-0.3, -0.3))
    api.ingest(capture(tmp_path, "new", [lone], received=DAY + "13:00:20Z"))
    api.server.hub.tick()
    expect(status).to_have_attribute("data-deltas", str(deltas + 1), timeout=WAIT)
    expect(row(page, "LIVE-1")).to_have_count(1)
    live = desk_media(page)
    page.reload()
    open_media(page, api)
    assert desk_media(page) == live  # a fresh REST view reads the same
    # A repeat delivery: a fresh snapshot (never a delta), and it too matches
    # a reload. The snapshot counter is not compared: the window's own
    # subscribe snapshot may still be arriving after the reload. The delta
    # counter cannot race that way.
    deltas = int(status.get_attribute("data-deltas"))
    api.ingest(capture(tmp_path, "repeat", [lone], received=DAY + "13:00:30Z"))
    api.server.hub.tick()
    page.wait_for_function(
        "() => document.getElementById('media-coverage').innerText"
        ".split('covered (1 item in this view)').length === 3",
        timeout=WAIT,
    )
    assert int(status.get_attribute("data-deltas")) == deltas  # a resnapshot, not a delta
    repeated = desk_media(page)
    page.reload()
    open_media(page, api)
    assert desk_media(page) == repeated


def test_media_table_is_accessible(api_server, page):
    api = demo(api_server)
    open_media(page, api)
    table = page.locator("#media-facts")
    assert table.locator("th[scope=col]").count() == 9
    assert table.locator("tbody th[scope=row]").count() == 8
    assert accessibility_problems(page) == []
