"""Package 4d on PostGIS: news and media items as their own evidence.

Every refusal has a working control beside it:

* an unapproved or unknown source is refused before anything is archived,
  beside the approved synthetic source;
* an exact repeat delivery adds a receipt, not an item, beside a real
  correction that adds a version;
* a correction or retraction keeps every version; a conflict stays unresolved;
* a late delivery keeps its source times and gets EYE's later receipt time;
* a provider outage is failed coverage, never "no news", beside a healthy
  empty capture that is a measured zero of items;
* a capture carrying a body, image or video, unsafe text, inconsistent
  rights or times, or a duplicate JSON key is refused whole before archiving,
  beside a clean capture that is stored;
* replay re-derives every item and catches a stray row.
"""

from __future__ import annotations

import copy
import json
import shutil

import jsonschema
import pytest
from conftest import AIS_DEMO, CAPTURES, REPO_ROOT
from eye.ingest.capture import CaptureRejected, archive, ingest, load_fixtures, verify_replay
from eye.storage.db import connect
from eye.storage.migrate import MIGRATIONS_DIR, migrate
from eye.wire import SCHEMA_PATH, validate_message
from pg8000.exceptions import DatabaseError
from synthetic_media import DAY, capture, item, licensed, place
from ws_client import WsClient, subscribe

EVENTS_DEMO = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "events" / "demo"
MEDIA_DEMO = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "media" / "demo"


def put(db, path) -> object:
    return ingest(db, path.read_bytes())


def items(db) -> list:
    return db.run(
        "SELECT m.item_id, v.version, v.is_current, m.status, v.receipt_count "
        "FROM eye.media_item m JOIN eye.media_item_version v USING (media_item_id) "
        "ORDER BY m.item_id, v.version, m.media_item_id"
    )


def coverage(db) -> list:
    return db.run(
        "SELECT state::text, metric_value, reason FROM eye.coverage "
        "WHERE metric_name = 'media_items' ORDER BY interval_start"
    )


# --- sources ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("gdelt", "candidate in docs/news-media-source-shortlist.md"),
        ("youtube-data", "candidate in docs/news-media-source-shortlist.md"),
        ("google-news", "candidate in docs/news-media-source-shortlist.md"),
        ("some-news-feed", "no approved row"),
    ],
)
def test_unapproved_or_unknown_sources_are_refused(db, tmp_path, source, message):
    path = capture(tmp_path, "refused", [item("N-1", DAY + "12:10:00Z")], source=source)
    with pytest.raises(CaptureRejected, match=message):
        archive(db, path.read_bytes())
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[0]]  # nothing archived
    # Control: the same items from the approved synthetic source are accepted.
    result = put(db, capture(tmp_path, "ok", [item("N-1", DAY + "12:10:00Z")]))
    assert (result.status, result.accepted, result.rejected) == ("committed", 1, 0)


def test_a_capture_not_marked_synthetic_or_in_the_wrong_format_is_refused(db, tmp_path):
    path = capture(tmp_path, "real", [item("N-1", DAY + "12:10:00Z")])
    doc = json.loads(path.read_text())
    for change, message in (
        ({"synthetic": False}, "synthetic"),
        ({"capture_format": "eye.synthetic-event-claims/1"}, "capture_format"),
        ({"layer": "road"}, "does not provide layer road"),
    ):
        with pytest.raises(CaptureRejected, match=message):
            archive(db, json.dumps({**doc, **change}).encode())
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[0]]
    assert put(db, path).status == "committed"  # control


# --- duplicates, late arrivals, corrections ---------------------------------------------------


def test_repeat_delivery_adds_a_receipt_not_an_item(db, tmp_path):
    story = item("DUP-1", DAY + "12:10:00Z", where=place(0.1, 0.1))
    put(db, capture(tmp_path, "first", [story]))
    put(db, capture(tmp_path, "again", [story], received=DAY + "13:00:20Z"))
    assert db.run("SELECT count(*) FROM eye.media_item") == [[1]]
    assert db.run("SELECT count(*) FROM eye.media_item_receipt") == [[2]]
    assert items(db) == [["DUP-1", 1, True, "published", 2]]
    # Control: changed content from the source is a new version, not a duplicate.
    fixed = item(
        "DUP-1",
        DAY + "12:10:00Z",
        status="corrected",
        revised=DAY + "12:40:00Z",
        headline="Invented report DUP-1 (corrected)",
        where=place(0.1, 0.1),
    )
    put(db, capture(tmp_path, "fixed", [fixed], received=DAY + "13:00:30Z"))
    assert items(db) == [
        ["DUP-1", 1, False, "published", 2],
        ["DUP-1", 2, True, "corrected", 1],
    ]


def test_corrections_retractions_and_conflicts_keep_every_version(db, tmp_path):
    first = item("C-1", DAY + "12:05:00Z")
    fixed = item("C-1", DAY + "12:05:00Z", status="corrected", revised=DAY + "12:30:00Z")
    gone = item("C-1", DAY + "12:05:00Z", status="retracted", revised=DAY + "12:50:00Z")
    a = item("X-1", DAY + "12:05:00Z", status="updated", revised=DAY + "12:20:00Z", headline="A")
    b = item("X-1", DAY + "12:05:00Z", status="updated", revised=DAY + "12:20:00Z", headline="B")
    put(db, capture(tmp_path, "all", [first, fixed, gone, a, b]))
    assert items(db) == [
        ["C-1", 1, False, "published", 1],
        ["C-1", 2, False, "corrected", 1],
        ["C-1", 3, True, "retracted", 1],
        ["X-1", 1, None, "updated", 1],  # two versions at one revision time: unknown,
        ["X-1", 1, None, "updated", 1],  # never an arbitrary choice
    ]
    # A later version resolves the conflict.
    c = item("X-1", DAY + "12:05:00Z", status="updated", revised=DAY + "13:20:00Z", headline="C")
    put(
        db,
        capture(
            tmp_path,
            "resolve",
            [c],
            start=DAY + "13:00:00Z",
            end=DAY + "14:00:00Z",
        ),
    )
    assert [r[1:3] for r in items(db) if r[0] == "X-1"] == [[1, False], [1, False], [2, True]]


def test_late_delivery_keeps_source_times_and_eye_receipt_time(db, tmp_path):
    story = item("LATE-1", DAY + "12:10:00Z")
    put(db, capture(tmp_path, "late", [story], received=DAY + "15:30:00Z"))
    assert db.run(
        "SELECT eye.iso_utc(m.first_published_time), eye.iso_utc(r.received_time) "
        "FROM eye.media_item m JOIN eye.media_item_receipt r USING (media_item_id)"
    ) == [[DAY + "12:10:00.000000Z", DAY + "15:30:00.000000Z"]]
    # Control: an item revised after the capture's window is not from that
    # window; the whole news capture is refused before anything is archived.
    outside = item("LATE-2", DAY + "12:10:00Z", status="updated", revised=DAY + "13:40:00Z")
    path = capture(tmp_path, "outside", [outside], received=DAY + "15:40:00Z")
    with pytest.raises(CaptureRejected, match="revised outside the request"):
        archive(db, path.read_bytes())
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[1]]  # only the first


# --- outages ---------------------------------------------------------------------------------


def test_outage_is_failed_coverage_and_an_empty_capture_a_measured_zero(db, tmp_path):
    put(db, capture(tmp_path, "down", [], status="timeout"))
    put(db, capture(tmp_path, "empty", [], start=DAY + "13:00:00Z", end=DAY + "14:00:00Z"))
    assert coverage(db) == [
        ["failed", None, "provider status timeout"],
        ["qualified", 0, None],  # control: a healthy, empty answer
    ]


def test_a_failed_attempt_carrying_items_is_refused(db, tmp_path):
    path = capture(tmp_path, "error", [item("E-1", DAY + "12:10:00Z")], status="error")
    with pytest.raises(CaptureRejected, match="status error carries no items"):
        archive(db, path.read_bytes())
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[0]]
    # Control: the adapter records the outage with no items.
    put(db, capture(tmp_path, "error-empty", [], status="error"))
    assert coverage(db) == [["failed", None, "provider status error"]]


# --- what an item may carry -----------------------------------------------------------------


def refused_each(db, tmp_path, name: str, bad: list[dict]) -> int:
    """Each bad item, beside one good item, refuses its whole capture before
    anything is archived; then the good item alone is stored (the control).
    Returns how many captures were refused."""
    good = item("GOOD", DAY + "12:10:00Z")
    refused = 0
    for index, one in enumerate(bad):
        path = capture(tmp_path, f"{name}-{index}", [good, one])
        with pytest.raises(CaptureRejected):
            archive(db, path.read_bytes())
        refused += 1
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[0]]
    assert put(db, capture(tmp_path, f"{name}-good", [good])).accepted == 1
    return refused


def raw_evidence_holds(db, text: str) -> bool:
    """Whether any archived raw capture contains ``text`` (the permanent record)."""
    rows = db.run("SELECT content FROM eye.raw_evidence")
    return any(text.encode("utf-8") in bytes(content) for (content,) in rows)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("body", "INVENTED-BODY-TEXT full article"),
        ("thumbnail", "INVENTED-THUMBNAIL-BYTES iVBORw0KGgo="),
        ("summary", "INVENTED-SUMMARY-TEXT an unreviewed field"),
    ],
)
def test_bodies_media_and_unreviewed_fields_are_never_archived(db, tmp_path, field, value):
    """Raw evidence is permanent, so such a capture is refused before archiving,
    not merely rejected item by item after its bytes are stored."""
    good = item("GOOD", DAY + "12:10:00Z")
    carrying = {**item("CARRY", DAY + "12:10:00Z", kind="image"), field: value}
    path = capture(tmp_path, "content", [good, carrying])
    with pytest.raises(CaptureRejected, match="metadata and a link only"):
        archive(db, path.read_bytes())
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[0]]
    assert not raw_evidence_holds(db, value.split()[0])
    # Control: the same capture without the field is archived and stored.
    put(db, capture(tmp_path, "clean", [good, item("CARRY", DAY + "12:10:00Z", kind="image")]))
    assert raw_evidence_holds(db, "https://news.invalid/carry")  # the search does find text
    assert db.run("SELECT item_id FROM eye.media_item ORDER BY 1") == [["CARRY"], ["GOOD"]]


def news_doc(tmp_path, name: str) -> dict:
    """A clean one-item news capture, as a document to alter."""
    good = item("CLEAN", DAY + "12:10:00Z", where=place(0.1, 0.1))
    return json.loads(capture(tmp_path, name, [good]).read_text(encoding="utf-8"))


def _leak_headline(doc, marker):
    doc["provider_response"]["items"][0]["headline"] = marker + "x" * 2000


def _leak_publisher(doc, marker):
    doc["provider_response"]["items"][0]["publisher"] = marker + "x" * 2000


def _leak_string_item(doc, marker):
    doc["provider_response"]["items"].append(marker + " a bare string item")


def _leak_place_key(doc, marker):
    doc["provider_response"]["items"][0]["place"]["note"] = marker


def _leak_rights_string(doc, marker):
    doc["provider_response"]["items"][0].update(rights="unknown", headline=marker)


def _leak_note(doc, marker):
    doc["note"] = marker + "x" * 2000


def _leak_response_key(doc, marker):
    doc["provider_response"]["raw_html"] = marker


def _leak_attempt_key(doc, marker):
    doc["attempt"]["provider_body"] = marker


def _leak_request_key(doc, marker):
    doc["request"]["query_text"] = marker


LEAK_ROUTES = {
    "over-long headline": _leak_headline,
    "over-long publisher": _leak_publisher,
    "string item": _leak_string_item,
    "extra key in place": _leak_place_key,
    "malformed rights with a headline": _leak_rights_string,
    "over-long envelope note": _leak_note,
    "extra key in provider_response": _leak_response_key,
    "extra key in attempt": _leak_attempt_key,
    "extra key in request": _leak_request_key,
}


@pytest.mark.parametrize("route", sorted(LEAK_ROUTES))
def test_no_route_archives_text_before_validation(db, tmp_path, route):
    """Raw evidence is permanent: a news capture is validated in full, envelope
    and every item, before archiving, and refused whole on any failure."""
    marker = "INVENTED-LEAK-" + route.replace(" ", "-").upper()
    doc = news_doc(tmp_path, "leak")
    LEAK_ROUTES[route](doc, marker)
    with pytest.raises(CaptureRejected):
        archive(db, json.dumps(doc).encode("utf-8"))
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[0]]
    assert not raw_evidence_holds(db, marker)
    # Control: the clean capture is archived and stored, and the same search
    # finds text that is really there.
    put(db, capture(tmp_path, "clean", [item("CLEAN", DAY + "12:10:00Z", where=place(0.1, 0.1))]))
    assert raw_evidence_holds(db, "https://news.invalid/clean")
    assert db.run("SELECT item_id FROM eye.media_item") == [["CLEAN"]]


def _splice(raw: bytes, anchor: bytes, inserted: bytes) -> bytes:
    """Insert raw bytes before the first ``anchor``: the duplicate is written
    as bytes, never produced by ``json.dumps``, which cannot repeat a key."""
    assert raw.count(anchor) >= 1, anchor
    return raw.replace(anchor, inserted + anchor, 1)


# Each route puts the marker in the FIRST of two equal keys. json.loads keeps
# the last (clean) value, so the old parser validated clean text while the
# archived bytes kept the marker.
DUPLICATE_ROUTES = {
    "duplicate top-level note": lambda raw, marker: _splice(
        raw, b'"note": ', b'"note": "' + marker + b'", '
    ),
    "duplicate item headline": lambda raw, marker: _splice(
        raw, b'"headline": ', b'"headline": "' + marker + b'", '
    ),
    "duplicate items key": lambda raw, marker: _splice(
        raw, b'"items": ', b'"items": ["' + marker + b"x" * 2048 + b'"], '
    ),
}


def two_item_capture(tmp_path, name: str) -> bytes:
    """Clean bytes with two items, so sibling objects share key names."""
    items = [
        item("CLEAN", DAY + "12:10:00Z", where=place(0.1, 0.1)),
        item("SIBLING", DAY + "12:20:00Z"),
    ]
    return capture(tmp_path, name, items).read_bytes()


@pytest.mark.parametrize("route", sorted(DUPLICATE_ROUTES))
def test_duplicate_json_keys_are_refused_before_archiving(db, tmp_path, route):
    marker = ("INVENTED-DUP-" + route.replace(" ", "-").upper()).encode("utf-8")
    raw = DUPLICATE_ROUTES[route](two_item_capture(tmp_path, "dup"), marker)
    assert marker in raw
    # The route is real: a plain parse keeps the last value and loses the marker.
    assert marker not in json.dumps(json.loads(raw)).encode("utf-8")
    with pytest.raises(CaptureRejected, match="duplicate JSON key"):
        archive(db, raw)
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[0]]
    assert not raw_evidence_holds(db, marker.decode("utf-8"))
    # Control: the same bytes without the repeat, where both items use the
    # same key names in separate objects, are archived and stored.
    ingest(db, two_item_capture(tmp_path, "clean"))
    assert raw_evidence_holds(db, "https://news.invalid/clean")
    assert db.run("SELECT item_id FROM eye.media_item ORDER BY 1") == [["CLEAN"], ["SIBLING"]]


def test_unknown_rights_headlines_are_never_stored(db, tmp_path):
    secret = "INVENTED-UNLICENSED-HEADLINE"
    unknown = item(
        "UNK", DAY + "12:10:00Z", kind="video", headline=secret, rights={"status": "unknown"}
    )
    path = capture(tmp_path, "unknown", [item("GOOD", DAY + "12:10:00Z"), unknown])
    with pytest.raises(CaptureRejected, match="unknown reuse rights but carries a headline"):
        archive(db, path.read_bytes())
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[0]]
    assert not raw_evidence_holds(db, secret)
    # Control: the adapter drops the headline, and the item is stored as a link.
    dropped = {**unknown, "headline": None}
    put(db, capture(tmp_path, "dropped", [dropped]))
    assert db.run("SELECT rights_status, headline, url FROM eye.media_item") == [
        ["unknown", None, "https://news.invalid/unk"]
    ]
    assert not raw_evidence_holds(db, secret)


def test_untrusted_text_is_bounded_and_stored_as_given(db, tmp_path):
    bad = [
        item("CTRL", DAY + "12:10:00Z", headline="Line one\nLine two"),
        item("BIDI", DAY + "12:10:00Z", headline="Safe ‮emag"),
        item("LONG", DAY + "12:10:00Z", headline="x" * 301),
        item("CREDS", DAY + "12:10:00Z", url="https://user:pw@news.invalid/a"),
        item("HTTP", DAY + "12:10:00Z", url="http://news.invalid/a"),
        item("SCRIPT", DAY + "12:10:00Z", url="javascript:alert(1)"),
    ]
    assert refused_each(db, tmp_path, "text", bad) == 6
    # Control: markup is only text; it is stored exactly as the source gave it.
    markup = '<img src=x onerror="alert(1)"> Invented & <b>bold</b> claim'
    put(db, capture(tmp_path, "markup", [item("MARKUP", DAY + "12:20:00Z", headline=markup)]))
    assert db.run("SELECT headline FROM eye.media_item WHERE item_id = 'MARKUP'") == [[markup]]


def test_rights_must_be_consistent(db, tmp_path):
    bad = [
        item("NO-ATTR", DAY + "12:10:00Z", rights={"status": "licensed", "licence": "CC-BY-4.0"}),
        item(
            "UNK-LIC",
            DAY + "12:10:00Z",
            headline=None,
            rights={"status": "unknown", "licence": "CC-BY-4.0"},
        ),
        item("NO-RIGHTS", DAY + "12:10:00Z", rights={"status": "free"}),
    ]
    assert refused_each(db, tmp_path, "rights", bad) == 3
    # Controls: each valid rights state is stored.
    put(
        db,
        capture(
            tmp_path,
            "rights-ok",
            [
                item("LIC", DAY + "12:20:00Z", kind="image", rights=licensed()),
                item("LINK", DAY + "12:20:00Z", rights={"status": "link_only"}),
                item(
                    "UNK",
                    DAY + "12:20:00Z",
                    kind="video",
                    headline=None,
                    rights={"status": "unknown"},
                ),
            ],
        ),
    )
    assert db.run(
        "SELECT item_id, rights_status, licence FROM eye.media_item "
        "WHERE item_id <> 'GOOD' ORDER BY item_id"
    ) == [["LIC", "licensed", "CC-BY-4.0"], ["LINK", "link_only", None], ["UNK", "unknown", None]]


def test_times_and_places_must_be_consistent(db, tmp_path):
    bad = [
        item("REV-EARLY", DAY + "12:30:00Z", status="updated", revised=DAY + "12:10:00Z"),
        item("PUB-LATER", DAY + "12:10:00Z", revised=DAY + "12:20:00Z"),  # published ≠ revised
        item("ART-CAPT", DAY + "12:10:00Z", captured=DAY + "12:00:00Z"),
        item("CAPT-AFTER", DAY + "12:10:00Z", kind="image", captured=DAY + "12:20:00Z"),
        item("ROLE", DAY + "12:10:00Z", where=place(0.1, 0.1, role="somewhere")),
        item("ZERO", DAY + "12:10:00Z", where=place(0.1, 0.1, precision=0)),
    ]
    assert refused_each(db, tmp_path, "times", bad) == 6
    # Control: old footage reposted later is valid evidence of what was claimed.
    old = item("OLD", DAY + "12:20:00Z", kind="video", captured="2024-05-01T09:00:00Z")
    put(db, capture(tmp_path, "old", [old]))
    assert db.run(
        "SELECT eye.iso_utc(capture_time_claimed) FROM eye.media_item WHERE item_id = 'OLD'"
    ) == [["2024-05-01T09:00:00.000000Z"]]


def test_database_refuses_inconsistent_rights_and_places(db):
    base = (
        "INSERT INTO eye.media_item (media_item_id, source_id, item_id, kind, status, "
        "first_published_time, revision_time, url, publisher, rights_status, licence, "
        "attribution, place_role, place_method, place, place_precision_m, content_sha256) "
        "VALUES (gen_random_uuid(), 'synthetic-news', :item, 'article', 'published', "
        "'2026-02-01T12:00:00Z', '2026-02-01T12:00:00Z', 'https://news.invalid/a', 'P', "
        ":rights, :licence, :attribution, :role, :method, "
        "ST_SetSRID(ST_MakePoint(0, 0), 4326), :precision, repeat('a', 64))"
    )
    good = {
        "rights": "licensed",
        "licence": "CC-BY-4.0",
        "attribution": "A",
        "role": "event_place",
        "method": "source_stated",
        "precision": 100,
    }
    for change in (
        {"attribution": None},
        {"rights": "unknown"},
        {"role": None},
        {"precision": None},
    ):
        with pytest.raises(DatabaseError, match="violates check constraint"):
            db.run(base, item="BAD", **{**good, **change})
    db.run(base, item="GOOD", **good)  # control
    assert db.run("SELECT count(*) FROM eye.media_item") == [[1]]


def test_database_refuses_a_headline_with_unknown_rights(db):
    insert = (
        "INSERT INTO eye.media_item (media_item_id, source_id, item_id, kind, status, "
        "first_published_time, revision_time, url, publisher, headline, rights_status, "
        "content_sha256) VALUES (gen_random_uuid(), 'synthetic-news', :item, 'article', "
        "'published', '2026-02-01T12:00:00Z', '2026-02-01T12:00:00Z', "
        "'https://news.invalid/a', 'P', :headline, :rights, repeat('a', 64))"
    )
    with pytest.raises(DatabaseError, match="unknown_rights_store_no_headline"):
        db.run(insert, item="UNK", headline="Invented headline", rights="unknown")
    # Controls: a link-only headline is stored; an unknown-rights item without one is too.
    db.run(insert, item="LINK", headline="Invented headline", rights="link_only")
    db.run(insert, item="UNK", headline=None, rights="unknown")
    assert db.run("SELECT item_id, headline FROM eye.media_item ORDER BY 1") == [
        ["LINK", "Invented headline"],
        ["UNK", None],
    ]


# --- replay, append-only, migration --------------------------------------------------------


def test_replay_rederives_every_item_and_catches_a_stray_row(make_db):
    ids = []
    for reverse in (False, True):
        conn = connect(make_db())
        migrate(conn)
        load_fixtures(conn, MEDIA_DEMO, reverse=reverse)
        assert verify_replay(conn) == []
        ids.append(
            conn.run(
                "SELECT media_item_id::text, item_id, version, is_current "
                "FROM eye.media_item_version ORDER BY media_item_id"
            )
        )
        if reverse:
            conn.run(
                "INSERT INTO eye.media_item (media_item_id, source_id, item_id, kind, status, "
                "first_published_time, revision_time, url, publisher, rights_status, "
                "content_sha256) VALUES (gen_random_uuid(), 'synthetic-news', 'STRAY', "
                "'article', 'published', now(), now(), 'https://news.invalid/s', 'P', "
                "'unknown', repeat('a', 64))"
            )
            problems = verify_replay(conn)
            assert len(problems) == 1 and "not derivable" in problems[0]
        conn.close()
    assert ids[0] == ids[1] and len(ids[0]) >= 8  # same ids and versions in either order


def test_items_and_receipts_are_append_only(db):
    load_fixtures(db, MEDIA_DEMO)
    assert db.run("SELECT count(*) > 0 FROM eye.media_item") == [[True]]  # control
    for statement in (
        "UPDATE eye.media_item SET headline = 'changed'",
        "DELETE FROM eye.media_item",
        "UPDATE eye.media_item_receipt SET adapter_version = 'x'",
        "DELETE FROM eye.media_item_receipt",
    ):
        with pytest.raises(DatabaseError, match="not permitted"):
            db.run(statement)


def test_media_items_are_kept_apart_from_event_claims(db):
    load_fixtures(db, EVENTS_DEMO)
    claims = db.run("SELECT count(*) FROM eye.event_claim")
    load_fixtures(db, MEDIA_DEMO)
    assert db.run("SELECT count(*) FROM eye.event_claim") == claims  # no claim added
    # No media table refers to an event table, or the other way round.
    references = db.run(
        "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint "
        "WHERE contype = 'f' AND (conrelid::regclass::text LIKE 'eye.media%' "
        "OR confrelid::regclass::text LIKE 'eye.media%') ORDER BY 1, 2"
    )
    assert references == [
        ["eye.media_item_receipt", "eye.capture_batch"],
        ["eye.media_item_receipt", "eye.media_item"],
        ["eye.media_item_receipt", "eye.raw_evidence"],
    ]


def table_digest(conn, name: str) -> list:
    sql = f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM eye.{name} t"  # noqa: S608
    return conn.run(sql)


def test_migration_0006_leaves_earlier_history_unchanged(make_db, tmp_path):
    before = tmp_path / "migrations"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("000[1-5]_*.sql")):
        shutil.copy(path, before / path.name)
    conn = connect(make_db())
    assert migrate(conn, before) == [1, 2, 3, 4, 5]
    load_fixtures(conn, CAPTURES)
    load_fixtures(conn, AIS_DEMO)
    load_fixtures(conn, EVENTS_DEMO)
    tables = (
        "capture_batch",
        "raw_evidence",
        "observation",
        "observation_receipt",
        "coverage",
        "feed_change",
        "event_claim",
        "event_claim_receipt",
    )
    prior = [table_digest(conn, name) for name in tables]
    assert migrate(conn) == [6]
    assert [table_digest(conn, name) for name in tables] == prior
    assert verify_replay(conn) == []
    conn.close()


# --- the API: REST snapshots ------------------------------------------------------------------

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VIEW = "/api/v0/snapshot?start=2026-02-01T12:00:00Z&end=2026-02-01T16:00:00Z"
AREA = (-0.5, -0.5, 0.5, 0.5)
HOURS = {"start": DAY + "12:00:00Z", "end": DAY + "16:00:00Z"}


def reference_valid(message: dict) -> bool:
    document = copy.deepcopy(SCHEMA)
    document["$ref"] = "#/$defs/ServerMessage"
    return jsonschema.Draft202012Validator(document).is_valid(message)


def snapshot(api, path: str = VIEW) -> dict:
    response, body = api.get(path)
    assert response.status == 200, body
    message = json.loads(body)
    assert validate_message(message, "ServerMessage") and reference_valid(message)
    return message


def media_of(message: dict, item_id: str) -> dict:
    (found,) = [m for m in message["media"] if m["item_id"] == item_id]
    return found


def current(item: dict) -> dict:
    (version,) = [v for v in item["versions"] if v["version_id"] == item["current_version_id"]]
    return version


def news_rows(message: dict) -> list:
    return sorted(
        (c["interval"]["start"], c["interval"]["end"], c["state"], c["metric"]["value"])
        for c in message["coverage"]
        if c["layer"] == "news"
    )


def pairs(message: dict) -> set:
    """Suggestions as ((item id, item id), basis), by the items' source ids."""
    ids = {m["id"]: m["item_id"] for m in message["media"]}
    return {
        (tuple(sorted(ids[i] for i in s["items"])), s["basis"])
        for s in message["media_suggestions"]
    }


def demo_media(api_server):
    api = api_server()
    for path in sorted(MEDIA_DEMO.glob("*.json")):
        api.ingest(path)
    return api


def test_demo_items_carry_source_times_rights_and_history(api_server):
    api = demo_media(api_server)
    view = snapshot(api)
    assert view["events"] == []  # news never becomes an event case
    assert sorted(m["item_id"] for m in view["media"]) == [
        "SN-A1",
        "SN-B1",
        "SN-C1",
        "SN-G1",
        "SN-L1",
        "SN-P1",
        "SN-V1",
        "SN-W1",
    ]
    for served in view["media"]:
        assert served["source"] == "synthetic-news"
        assert served["source_label"].startswith("Synthetic news and media")
    # A correction keeps every version, each with its own times.
    a1 = media_of(view, "SN-A1")
    assert a1["standing"] == "corrected"
    assert [(v["version"], v["is_current"], v["place"]["precision_m"]) for v in a1["versions"]] == [
        (1, False, 2000.0),
        (2, True, 500.0),
    ]
    assert [v["received_time"] for v in a1["versions"]] == [DAY + "13:00:10Z", DAY + "14:00:10Z"]
    # A repeat delivery is two receipts of one version.
    (b1,) = media_of(view, "SN-B1")["versions"]
    assert len(b1["evidence_batch_ids"]) == 2 and b1["received_time"] == DAY + "13:00:10Z"
    # A late delivery keeps its publication time and EYE's later receipt time.
    (l1,) = media_of(view, "SN-L1")["versions"]
    assert (l1["first_published_time"], l1["received_time"]) == (
        DAY + "13:20:00Z",
        DAY + "15:30:00Z",
    )
    # Unknown rights: a link only, headline withheld. Controls beside it.
    v1 = current(media_of(view, "SN-V1"))
    assert (v1["rights"], v1["headline"], v1["headline_withheld"]) == (
        {"status": "unknown"},
        None,
        True,
    )
    assert v1["url"] == "https://clips.invalid/watch/abc123"
    assert v1["capture_time_claimed"] == "2024-05-01T09:00:00Z"  # old footage, as claimed
    p1 = current(media_of(view, "SN-P1"))
    assert p1["rights"]["licence"] == "CC-BY-4.0" and p1["headline"] is not None
    assert current(media_of(view, "SN-B1"))["headline"] is not None  # link_only keeps it
    # Places keep their role and method.
    assert current(media_of(view, "SN-W1"))["place"]["role"] == "publisher_location"
    assert current(media_of(view, "SN-G1"))["place"]["method"] == "automated_geocode"
    # News coverage: reporting windows, the outage failed, the rest unknown.
    assert news_rows(view) == [
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 1),  # the repeat of SN-B1
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 6),
        (DAY + "13:00:00Z", DAY + "14:00:00Z", "qualified", 1),
        (DAY + "13:00:00Z", DAY + "14:00:00Z", "qualified", 1),
        (DAY + "14:00:00Z", DAY + "15:00:00Z", "failed", None),  # the outage, never zero
        (DAY + "15:00:00Z", DAY + "16:00:00Z", "unknown", None),  # no capture at all
    ]


def test_suggestions_link_items_but_never_merge_them(api_server):
    api = demo_media(api_server)
    view = snapshot(api)
    found = pairs(view)
    # A syndicated copy is flagged as a copy, never as a second report.
    assert (("SN-A1", "SN-C1"), "syndicated_copy") in found
    assert not any(p == ("SN-A1", "SN-C1") and b == "place_and_time" for p, b in found)
    # Independent publishers near the same stated place and time: a suggestion only.
    assert (("SN-A1", "SN-B1"), "place_and_time") in found
    assert all(s["status"] == "suggestion" for s in view["media_suggestions"])
    # Both stay separate items, each with its own history.
    assert len({m["id"] for m in view["media"]}) == len(view["media"])
    # Negatives: a publisher's city, an automated geocode and a far-away
    # video never take part in a place-and-time suggestion.
    involved = {i for p, b in found if b == "place_and_time" for i in p}
    assert involved.isdisjoint({"SN-W1", "SN-G1", "SN-V1"})


def test_suggestion_rules_each_have_a_control(api_server, tmp_path):
    api = api_server()
    near = place(0.1, 0.1, 2000)
    items = [
        item("X-1", DAY + "12:10:00Z", publisher="Invented One", where=near),
        item("X-2", DAY + "12:20:00Z", publisher="Invented Two", where=place(0.11, 0.1, 2000)),
        item("SAME", DAY + "12:20:00Z", publisher="invented one", where=near),  # same publisher
        item("FAR", DAY + "12:20:00Z", publisher="Invented Three", where=place(0.3, 0.1, 2000)),
        item(
            "AUTO",
            DAY + "12:20:00Z",
            publisher="Invented Four",
            where=place(0.1, 0.1, 2000, method="automated_geocode"),
        ),
        item(
            "CITY",
            DAY + "12:20:00Z",
            publisher="Invented Five",
            where=place(0.1, 0.1, 2000, role="publisher_location"),
        ),
    ]
    api.ingest(capture(tmp_path, "rules", items))
    late = item("LATER", DAY + "18:30:00Z", publisher="Invented Six", where=near)
    api.ingest(capture(tmp_path, "later", [late], start=DAY + "18:00:00Z", end=DAY + "19:00:00Z"))
    view = snapshot(api, "/api/v0/snapshot?start=2026-02-01T12:00:00Z&end=2026-02-01T19:00:00Z")
    found = {p for p, _ in pairs(view)}
    assert ("X-1", "X-2") in found  # control: independent, near, close in time
    assert ("SAME", "X-2") in found  # SAME and X-2 differ in publisher: also a suggestion
    assert ("SAME", "X-1") not in found  # one publisher is not two reports
    assert not any("FAR" in p or "AUTO" in p or "CITY" in p for p in found)
    assert not any("LATER" in p for p in found)  # more than six hours apart


def test_outage_is_unknown_and_an_empty_capture_a_zero_of_items(api_server, tmp_path):
    api = api_server()
    api.ingest(capture(tmp_path, "empty", []))
    api.ingest(
        capture(
            tmp_path, "down", [], start=DAY + "13:00:00Z", end=DAY + "14:00:00Z", status="error"
        )
    )
    view = snapshot(api)
    assert view["media"] == []
    assert news_rows(view) == [
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 0),  # a real zero of items
        (DAY + "13:00:00Z", DAY + "14:00:00Z", "failed", None),  # never "no news"
        (DAY + "14:00:00Z", DAY + "16:00:00Z", "unknown", None),
    ]
    reasons = {c["reason"] for c in view["coverage"] if c["layer"] == "news" and "reason" in c}
    assert "no news source covers this area and time; items unknown, not absent" in reasons


def test_item_limit_is_refused_by_name(api_server, tmp_path):
    api = api_server(api={"max_media": 2})
    api.ingest(capture(tmp_path, "three", [item(f"L-{i}", DAY + "12:10:00Z") for i in range(3)]))
    response, body = api.get(VIEW)
    assert response.status == 413 and "more than 2 news and media items" in body.decode()
    # Control: a narrower interval holds two and is served.
    api.ingest(
        capture(
            tmp_path,
            "later",
            [item("L-9", DAY + "13:10:00Z")],
            start=DAY + "13:00:00Z",
            end=DAY + "14:00:00Z",
        )
    )
    narrow = snapshot(api, "/api/v0/snapshot?start=2026-02-01T13:00:00Z&end=2026-02-01T14:00:00Z")
    assert [m["item_id"] for m in narrow["media"]] == ["L-9"]


def crowded(tmp_path, name: str, count: int):
    """``count`` items from different publishers at one stated place within an
    hour: every pair qualifies as a place-and-time suggestion."""
    items = [
        item(
            f"C-{i:02d}",
            DAY + f"12:{i:02d}:00Z",
            publisher=f"Synthetic Publisher {i:02d}",
            where=place(0.1, 0.1),
        )
        for i in range(count)
    ]
    return capture(tmp_path, name, items)


def test_suggestion_limit_is_refused_by_name_never_cut_short(api_server, tmp_path):
    """33 items give 528 qualifying pairs, over the wire limit of 500. The
    view is refused with the count, rather than served with 500 and no sign
    that 28 are missing."""
    api = api_server()
    api.ingest(crowded(tmp_path, "crowded", 33))
    response, body = api.get(VIEW)
    assert response.status == 413
    error = validate_message(body, "ServerMessage")
    assert "more than 500 news and media suggestions (528 qualify)" in error["error"]
    # The WebSocket subscription is refused the same way, never sent 500.
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOURS))
    reply = client.recv()
    client.close()
    assert reply["kind"] == "error" and "(528 qualify)" in reply["error"]


def test_suggestions_under_the_limit_are_served_in_full(api_server, tmp_path):
    """Control: 32 items give 496 pairs, all served."""
    api = api_server()
    api.ingest(crowded(tmp_path, "crowded", 32))
    view = snapshot(api)
    assert len(view["media"]) == 32
    assert len(view["media_suggestions"]) == 32 * 31 // 2 == 496
    assert {s["status"] for s in view["media_suggestions"]} == {"suggestion"}


# --- the API: live updates ------------------------------------------------------------------


def rows(message: dict, key: str) -> list:
    return sorted(json.dumps(c, sort_keys=True) for c in message[key])


def test_live_items_arrive_as_deltas_and_changes_resnapshot(api_server, tmp_path):
    api = api_server()
    # Cover the whole view first, so later captures do not change the gaps.
    api.ingest(capture(tmp_path, "cover", [], end=DAY + "16:00:00Z"))
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOURS))
    base = client.recv()
    assert base["kind"] == "snapshot" and base["media"] == []
    # A new, unmatched item is an ordinary delta, and live equals REST.
    lone = item("LIVE-1", DAY + "12:20:00Z", where=place(-0.3, -0.3))
    api.ingest(capture(tmp_path, "new", [lone], received=DAY + "13:00:20Z"))
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and reference_valid(delta)
    assert [m["item_id"] for m in delta["media_upserted"]] == ["LIVE-1"]
    rest = snapshot(api)
    assert rows(delta, "media_upserted") == rows(rest, "media")
    assert sorted(
        json.dumps(c, sort_keys=True) for c in base["coverage"] + delta["coverage_upserted"]
    ) == rows(rest, "coverage")
    # A late delivery of another unmatched item is a delta too.
    late = item("LIVE-2", DAY + "12:40:00Z", where=place(0.3, -0.3))
    api.ingest(capture(tmp_path, "late", [late], received=DAY + "15:45:00Z"))
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta"
    (version,) = delta["media_upserted"][0]["versions"]
    assert version["received_time"] == DAY + "15:45:00Z"
    # A repeat delivery changes an earlier row's receipts: a fresh snapshot.
    api.ingest(capture(tmp_path, "repeat", [lone], received=DAY + "13:00:30Z"))
    api.server.hub.tick()
    assert client.recv()["kind"] == "resync_required"
    repeated = client.recv()
    assert rows(repeated, "media") == rows(snapshot(api), "media")
    # An item that matches another changes the suggestions: a fresh snapshot.
    near = item("LIVE-3", DAY + "12:30:00Z", publisher="Invented Other", where=place(-0.3, -0.3))
    api.ingest(capture(tmp_path, "near", [near], received=DAY + "13:00:40Z"))
    api.server.hub.tick()
    assert client.recv()["kind"] == "resync_required"
    matched = client.recv()
    assert pairs(matched) == {(("LIVE-1", "LIVE-3"), "place_and_time")} == pairs(snapshot(api))
    # A correction moving an item out of view cannot be an upsert: resnapshot.
    moved = item(
        "LIVE-2",
        DAY + "12:40:00Z",
        status="corrected",
        revised=DAY + "13:10:00Z",
        where=place(1.3, 1.3),
    )
    api.ingest(
        capture(
            tmp_path,
            "moved",
            [moved],
            start=DAY + "13:00:00Z",
            end=DAY + "14:00:00Z",
            bbox=(-2, -2, 2, 2),
        )
    )
    api.server.hub.tick()
    assert client.recv()["kind"] == "resync_required"
    fresh = client.recv()
    assert "LIVE-2" not in {m["item_id"] for m in fresh["media"]}
    assert rows(fresh, "media") == rows(snapshot(api), "media")
    client.close()


# --- no model and no network on the way in ------------------------------------------------

FORBIDDEN_IMPORTS = ("socket", "http", "urllib.request", "requests", "httpx", "anthropic", "openai")


def ingest_imports(path) -> set[str]:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def forbidden(names: set[str]) -> set[str]:
    return {n for n in names for f in FORBIDDEN_IMPORTS if n == f or n.startswith(f + ".")}


def test_ingestion_imports_no_network_client_and_no_model(tmp_path):
    ingest_dir = REPO_ROOT / "backend" / "eye" / "ingest"
    for path in sorted(ingest_dir.glob("*.py")):
        assert forbidden(ingest_imports(path)) == set(), path.name
    # Control: the same check flags a planted module that would call out.
    planted = tmp_path / "planted.py"
    planted.write_text("import urllib.request\nfrom anthropic import Anthropic\n", encoding="utf-8")
    assert forbidden(ingest_imports(planted)) == {"urllib.request", "anthropic"}
