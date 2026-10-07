"""Wire schema: valid and invalid messages on both sides, cross-checked.

Three independent implementations judge the same corpus: the Python runtime
validator, the compiled browser validator (run under Node) and the reference
``jsonschema`` library. Every valid case must pass all three and every invalid
case must fail all three, except documented reference divergences.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest
from conftest import REPO_ROOT
from eye.api import feed
from eye.wire import (
    SCHEMA_PATH,
    SCHEMA_VERSION,
    V1_SCHEMA_PATH,
    V2_SCHEMA_PATH,
    V3_SCHEMA_PATH,
    WireValidationError,
    load_schema,
    validate_message,
    wire_version_of,
)
from eye.wire.validate import SchemaDefinitionError, WireSchema

CORPUS = REPO_ROOT / "tests" / "fixtures" / "wire"
VALID = sorted((CORPUS / "valid").glob("*.json"))
INVALID = sorted((CORPUS / "invalid").glob("*.json"))
NODE_CHECK = REPO_ROOT / "tests" / "support" / "wire_check.mjs"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

# Raw-text cases the JSON corpus cannot express. Python's reference library
# is not asked about these; both EYE validators must refuse them.
RAW_INVALID = {
    "not-json": "{not json",
    "nan-number": '{"schema_version":"eye.wire/4","kind":"error","status":NaN,"error":"x"}',
    "trailing-newline-in-timestamp": json.dumps(
        {
            "schema_version": "eye.wire/4",
            "kind": "resync_required",
            "reason": "gap",
            "last_cursor": "c-0001\n",
        }
    ),
}
RAW_VALID = {
    "health": '{"schema_version":"eye.wire/4","kind":"health","status":"ok",'
    '"mode":"demo","synthetic":true}',
}


def case(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def reference_valid(entry: str, message: object) -> bool:
    document = copy.deepcopy(SCHEMA)
    document["$ref"] = f"#/$defs/{entry}"
    return jsonschema.Draft202012Validator(document).is_valid(message)


def test_corpus_is_not_empty():
    assert len(VALID) >= 8 and len(INVALID) >= 20


def test_reference_accepts_schema_as_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_messages_pass_python_and_reference(path):
    test = case(path)
    validate_message(json.dumps(test["message"]), test["entry"])
    assert reference_valid(test["entry"], test["message"])


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_messages_fail_python_and_reference(path):
    test = case(path)
    with pytest.raises(WireValidationError):
        validate_message(json.dumps(test["message"]), test["entry"])
    if "reference_divergence" in test:
        # Documented: the reference does not assert "format". Pin that, so a
        # change in the reference is noticed rather than silently absorbed.
        assert reference_valid(test["entry"], test["message"]), test["reference_divergence"]
    else:
        assert not reference_valid(test["entry"], test["message"]), "reference accepted it"


CALENDAR = {
    "2024-02-29T00:00:00Z": True,
    "2000-02-29T00:00:00Z": True,
    "2026-12-31T23:59:59.5Z": True,
    "2026-01-31T00:00:00Z": True,
    "2026-02-28T00:00:00Z": True,
    "2026-02-29T00:00:00Z": False,
    "2026-02-30T00:00:00Z": False,
    "1900-02-29T00:00:00Z": False,
    "2026-04-31T00:00:00Z": False,
    "2026-06-31T00:00:00Z": False,
    "2026-11-31T00:00:00Z": False,
    # Year 0000 is refused on both sides; year 0001 is the positive control.
    "0000-01-01T00:00:00Z": False,
    "0000-02-29T00:00:00Z": False,
    "0001-01-01T00:00:00Z": True,
    "9999-12-31T23:59:59Z": True,
}


def test_calendar_rule_agrees_across_python_and_browser(tmp_path):
    """Python's datetime and the browser's own leap-year rule are independent checks."""
    base = case(CORPUS / "valid" / "snapshot.json")["message"]
    files = {}
    for index, (value, expected) in enumerate(CALENDAR.items()):
        message = {**base, "generated_at": value}
        python_ok = not load_schema().errors(message, "ServerMessage")
        assert python_ok is expected, value
        target = tmp_path / f"date-{index}.json"
        target.write_text(json.dumps({"entry": "ServerMessage", "message": message}))
        files[str(target)] = expected
    node = shutil.which("node")
    assert node, "Node.js is required (scripts/setup.sh)"
    result = subprocess.run(
        [node, str(NODE_CHECK), *files], capture_output=True, text=True, timeout=60, check=True
    )
    verdicts = json.loads(result.stdout)
    assert {f: verdicts[f]["valid"] for f in files} == files


def test_browser_validator_agrees_on_corpus(tmp_path):
    node = shutil.which("node")
    assert node, "Node.js is required (scripts/setup.sh)"
    assert (REPO_ROOT / "web" / "dist" / "wire-validate.js").is_file(), "run scripts/setup.sh"
    raw_files = []
    for name, text in {**RAW_INVALID, **RAW_VALID}.items():
        entry = "ServerMessage"
        target = tmp_path / f"{name}.json"
        target.write_text(json.dumps({"entry": entry, "raw": text}), encoding="utf-8")
        raw_files.append(target)
    files = [*VALID, *INVALID, *raw_files]
    result = subprocess.run(
        [node, str(NODE_CHECK), *map(str, files)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    verdicts = json.loads(result.stdout)
    for path in VALID:
        assert verdicts[str(path)]["valid"], (path.stem, verdicts[str(path)]["errors"])
    for path in INVALID:
        assert not verdicts[str(path)]["valid"], path.stem
    for name in RAW_INVALID:
        assert not verdicts[str(tmp_path / f"{name}.json")]["valid"], name
    for name in RAW_VALID:
        assert verdicts[str(tmp_path / f"{name}.json")]["valid"], name


@pytest.mark.parametrize("name", sorted(RAW_INVALID))
def test_python_refuses_raw_invalid_text(name):
    with pytest.raises(WireValidationError):
        validate_message(RAW_INVALID[name], "ServerMessage")


def test_python_accepts_raw_valid_text():
    for text in RAW_VALID.values():
        validate_message(text, "ServerMessage")


def test_message_size_bound():
    text = RAW_VALID["health"]
    validate_message(text, "ServerMessage", max_bytes=len(text))
    with pytest.raises(WireValidationError, match="limit"):
        validate_message(text, "ServerMessage", max_bytes=len(text) - 1)


def test_schema_with_unsupported_keyword_is_refused():
    WireSchema(copy.deepcopy(SCHEMA))  # positive control
    broken = copy.deepcopy(SCHEMA)
    broken["$defs"]["Timestamp"]["contentEncoding"] = "base64"
    with pytest.raises(SchemaDefinitionError, match="unsupported keyword"):
        WireSchema(broken)


def test_schema_with_unsupported_format_is_refused():
    broken = copy.deepcopy(SCHEMA)
    broken["$defs"]["Timestamp"]["format"] = "email"
    with pytest.raises(SchemaDefinitionError, match="unsupported format"):
        WireSchema(broken)


def test_schema_with_open_object_is_refused():
    broken = copy.deepcopy(SCHEMA)
    del broken["$defs"]["HealthMessage"]["additionalProperties"]
    with pytest.raises(SchemaDefinitionError, match="additionalProperties"):
        WireSchema(broken)


def test_generated_browser_files_are_current():
    result = subprocess.run(
        [str(REPO_ROOT / ".venv" / "bin" / "python"), "scripts/gen_wire_types.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_demo_fixture_is_a_valid_snapshot():
    fixture = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "demo_snapshot.json"
    message = validate_message(fixture.read_bytes(), "ServerMessage")
    assert message["kind"] == "snapshot"
    assert load_schema() is load_schema()


# --- the current version beside the unchanged earlier versions -------------------------

# Each earlier schema exactly as merged, with the corpus it was merged with.
# Nothing serves them.
FROZEN = {
    "eye.wire/1": (
        V1_SCHEMA_PATH,
        "9761a30a4b3c5609602e9b2caa8411112cd9940662f5f9051e160f0ff2d14a73",
        REPO_ROOT / "tests" / "fixtures" / "wire-v1",
    ),
    "eye.wire/2": (
        V2_SCHEMA_PATH,
        "63fa0917ef2fe5dc1fa57bc4bab8adf7c844bd5a3453be6b349a1aeaa1e5d149",
        REPO_ROOT / "tests" / "fixtures" / "wire-v2",
    ),
    "eye.wire/3": (
        V3_SCHEMA_PATH,
        "6c3a3c89d294d7e0e7f42a5e334177c29bab94cf74869afe6077746fb6561c20",
        REPO_ROOT / "tests" / "fixtures" / "wire-v3",
    ),
}


def frozen_valid(version: str, entry: str, message: object) -> bool:
    """Both judges of an unchanged earlier schema; they must agree."""
    path = FROZEN[version][0]
    document = copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
    python = not load_schema(path).errors(message, entry)
    document["$ref"] = f"#/$defs/{entry}"
    assert python is jsonschema.Draft202012Validator(document).is_valid(message), (entry, message)
    return python


MEDIA_KEYS = ("media", "media_suggestions", "media_upserted")


def relabel(message: dict, version: str) -> dict:
    """The message under another version label. For an earlier version the
    empty media arrays eye.wire/4 added are dropped (they are the new shape,
    tested separately); for the current version they are added."""
    out = {**copy.deepcopy(message), "schema_version": version}
    if version == SCHEMA_VERSION:
        if out.get("kind") == "snapshot":
            out = {**out, "media": out.get("media", []), "media_suggestions": []}
        if out.get("kind") == "delta":
            out = {**out, "media_upserted": out.get("media_upserted", [])}
    else:
        for key in MEDIA_KEYS:
            if out.get(key) == []:
                del out[key]
    return out


def has_events(message: dict) -> bool:
    return bool(message.get("events") or message.get("events_upserted"))


def has_media(message: dict) -> bool:
    return any(message.get(key) for key in MEDIA_KEYS)


def new_since(version: str, message: dict) -> bool:
    """Shapes the given earlier version cannot carry.

    eye.wire/2 added conflict tracks and transit counts; eye.wire/3 replaced
    the event shape with event-ledger cases; eye.wire/4 added news and media
    items, their suggestions and news coverage rows.
    """
    news = any(c.get("layer") == "news" for c in message.get("coverage", []))
    if has_media(message) or news:
        return True
    if version == "eye.wire/3":
        return False
    if has_events(message):
        return True
    if version == "eye.wire/2":
        return False
    tracks = message.get("tracks", []) + message.get("tracks_upserted", [])
    return message.get("kind") == "transits" or any("conflicts" in t for t in tracks)


@pytest.mark.parametrize("version", sorted(FROZEN))
def test_earlier_schema_is_the_merged_file_unchanged(version):
    import hashlib

    path, sha, _ = FROZEN[version]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
    assert json.loads(path.read_text())["$defs"]["SchemaVersion"]["const"] == version
    assert SCHEMA["$defs"]["SchemaVersion"]["const"] == SCHEMA_VERSION == "eye.wire/4"
    assert path != SCHEMA_PATH


@pytest.mark.parametrize("version", sorted(FROZEN))
def test_earlier_corpus_still_judged_as_merged(version):
    """Positive control for every frozen-version check below."""
    corpus = FROZEN[version][2]
    valid = sorted((corpus / "valid").glob("*.json"))
    invalid = sorted((corpus / "invalid").glob("*.json"))
    assert len(valid) >= 8 and len(invalid) >= 20
    for path in valid:
        test = case(path)
        assert frozen_valid(version, test["entry"], test["message"]), path.stem
    for path in invalid:
        test = case(path)
        python = not load_schema(FROZEN[version][0]).errors(test["message"], test["entry"])
        assert not python, path.stem


@pytest.mark.parametrize("version", sorted(FROZEN))
def test_earlier_validator_refuses_every_current_message(version):
    for path in VALID:
        test = case(path)
        assert not frozen_valid(version, test["entry"], test["message"]), path.stem


@pytest.mark.parametrize("version", sorted(FROZEN))
def test_new_shapes_are_refused_by_earlier_versions_even_relabelled(version):
    """The new shapes, not only the label, are what an earlier version cannot carry."""
    new = [p for p in VALID if new_since(version, case(p)["message"])]
    expected = {"media-snapshot", "media-delta"}
    if version != "eye.wire/3":
        expected |= {
            "event-review-candidate",
            "event-unresolved-conflict",
            "event-linked-with-last-observed",
            "snapshot",
        }
    assert {p.stem for p in new} >= expected
    for path in new:
        test = case(path)
        assert not frozen_valid(version, test["entry"], relabel(test["message"], version)), (
            path.stem
        )
    # Control: a current message without those shapes, relabelled, is valid
    # in the earlier version: nothing else about it changed.
    unchanged = [p for p in VALID if not new_since(version, case(p)["message"])]
    assert {p.stem for p in unchanged} >= {"snapshot-empty", "delta", "subscribe", "health"}
    for path in unchanged:
        test = case(path)
        assert frozen_valid(version, test["entry"], relabel(test["message"], version)), path.stem


@pytest.mark.parametrize("version", sorted(FROZEN))
def test_current_validator_refuses_earlier_messages(version):
    corpus = FROZEN[version][2]
    for path in sorted((corpus / "valid").glob("*.json")):
        test = case(path)
        with pytest.raises(WireValidationError):
            validate_message(json.dumps(test["message"]), test["entry"])
        assert wire_version_of(json.dumps(test["message"])) == version
        relabelled = relabel(test["message"], SCHEMA_VERSION)
        if has_events(test["message"]) and version != "eye.wire/3":
            # The old event shape is exactly what version 3 replaced.
            with pytest.raises(WireValidationError):
                validate_message(json.dumps(relabelled), test["entry"])
        else:
            # Control: every other earlier shape is still a current shape.
            validate_message(json.dumps(relabelled), test["entry"])


def test_wire_version_is_read_without_trusting_the_message():
    assert wire_version_of('{"schema_version":"eye.wire/1","kind":"nonsense"}') == "eye.wire/1"
    assert wire_version_of(RAW_VALID["health"]) == SCHEMA_VERSION  # control
    for text in (
        "{not json",
        "[1]",
        '{"schema_version": 2}',
        '{"schema_version": "eye.wire/1 <b>"}',
        '{"schema_version": "eye.wire/0"}',
        '{"kind": "health"}',
    ):
        assert wire_version_of(text) is None, text
    big = '{"schema_version":"eye.wire/1","pad":"' + "x" * 64 + '"}'
    assert wire_version_of(big) == "eye.wire/1"
    assert wire_version_of(big, max_bytes=len(big) - 1) is None


def test_server_limits_match_the_schema():
    defs = SCHEMA["$defs"]
    assert defs["PositionConflict"]["properties"]["claims"]["maxItems"] == feed.MAX_CONFLICT_CLAIMS
    claim = defs["ConflictClaim"]["properties"]["evidence_batch_ids"]
    assert claim["maxItems"] == feed.MAX_CLAIM_EVIDENCE
    for shape in ("TrackRouted", "TrackUnresolved"):
        props = defs[shape]["properties"]
        assert props["conflicts"]["maxItems"] == feed.MAX_TRACK_CONFLICTS
    assert defs["TrackRouted"]["properties"]["points"]["maxItems"] == feed.MAX_TRACK_POINTS


def test_media_limits_match_the_schema():
    from eye.api import media
    from eye.ingest import media_items

    defs = SCHEMA["$defs"]
    assert defs["MediaItem"]["properties"]["versions"]["maxItems"] == media.MAX_ITEM_VERSIONS
    version = defs["MediaVersion"]["properties"]
    assert version["evidence_batch_ids"]["maxItems"] == media.MAX_EVIDENCE
    assert version["headline"]["maxLength"] == media_items.MAX_HEADLINE
    assert defs["MediaName"]["maxLength"] == media_items.MAX_NAME
    assert defs["HttpsUrl"]["maxLength"] == media_items.MAX_URL
    snapshot = defs["SnapshotMessage"]["properties"]
    assert snapshot["media_suggestions"]["maxItems"] == media.MAX_SUGGESTIONS
    assert set(defs["MediaPlace"]["properties"]["role"]["enum"]) == media_items.PLACE_ROLES
    assert set(defs["MediaPlace"]["properties"]["method"]["enum"]) == media_items.PLACE_METHODS
    kinds = defs["MediaItem"]["properties"]["kind"]["enum"]
    assert set(kinds) == media_items.KINDS
