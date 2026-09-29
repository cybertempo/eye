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
    "nan-number": '{"schema_version":"eye.wire/2","kind":"error","status":NaN,"error":"x"}',
    "trailing-newline-in-timestamp": json.dumps(
        {
            "schema_version": "eye.wire/2",
            "kind": "resync_required",
            "reason": "gap",
            "last_cursor": "c-0001\n",
        }
    ),
}
RAW_VALID = {
    "health": '{"schema_version":"eye.wire/2","kind":"health","status":"ok",'
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


# --- version 2 beside the unchanged version 1 -----------------------------------------

# The version-1 schema exactly as merged (main at b62cf4a). Nothing serves it.
V1_SHA256 = "9761a30a4b3c5609602e9b2caa8411112cd9940662f5f9051e160f0ff2d14a73"
V1_CORPUS = REPO_ROOT / "tests" / "fixtures" / "wire-v1"
V1_VALID = sorted((V1_CORPUS / "valid").glob("*.json"))
V1_INVALID = sorted((V1_CORPUS / "invalid").glob("*.json"))
V1_SCHEMA = json.loads(V1_SCHEMA_PATH.read_text(encoding="utf-8"))


def v1_reference_valid(entry: str, message: object) -> bool:
    document = copy.deepcopy(V1_SCHEMA)
    document["$ref"] = f"#/$defs/{entry}"
    return jsonschema.Draft202012Validator(document).is_valid(message)


def v1_valid(entry: str, message: object) -> bool:
    """Both judges of the unchanged v1 schema; they must agree."""
    python = not load_schema(V1_SCHEMA_PATH).errors(message, entry)
    assert python is v1_reference_valid(entry, message), (entry, message)
    return python


def relabel(message: dict, version: str) -> dict:
    return {**copy.deepcopy(message), "schema_version": version}


def uses_v2_shapes(message: dict) -> bool:
    """Conflict tracks and transit counts exist only in eye.wire/2."""
    tracks = message.get("tracks", []) + message.get("tracks_upserted", [])
    return message.get("kind") == "transits" or any("conflicts" in t for t in tracks)


def test_v1_schema_is_the_merged_file_unchanged():
    import hashlib

    assert hashlib.sha256(V1_SCHEMA_PATH.read_bytes()).hexdigest() == V1_SHA256
    assert V1_SCHEMA["$defs"]["SchemaVersion"]["const"] == "eye.wire/1"
    assert SCHEMA["$defs"]["SchemaVersion"]["const"] == SCHEMA_VERSION == "eye.wire/2"
    assert SCHEMA_PATH != V1_SCHEMA_PATH


def test_v1_corpus_still_judged_as_merged_by_the_v1_validator():
    """Positive control for every v1 check below: the v1 judges still work."""
    assert len(V1_VALID) >= 8 and len(V1_INVALID) >= 20
    for path in V1_VALID:
        test = case(path)
        assert v1_valid(test["entry"], test["message"]), path.stem
    for path in V1_INVALID:
        test = case(path)
        python = not load_schema(V1_SCHEMA_PATH).errors(test["message"], test["entry"])
        assert not python, path.stem
        if "reference_divergence" not in test:
            assert not v1_reference_valid(test["entry"], test["message"]), path.stem


def test_v1_validator_refuses_every_v2_message():
    for path in VALID:
        test = case(path)
        assert not v1_valid(test["entry"], test["message"]), path.stem


def test_conflict_and_transit_shapes_are_refused_by_v1_even_relabelled():
    """The new shapes, not only the label, are what version 1 cannot carry."""
    new_shapes = [p for p in VALID if uses_v2_shapes(case(p)["message"])]
    assert {p.stem for p in new_shapes} >= {
        "track-with-conflict",
        "track-only-conflicts",
        "conflict-with-20-claims",
        "transits",
    }
    for path in new_shapes:
        test = case(path)
        assert not v1_valid(test["entry"], relabel(test["message"], "eye.wire/1")), path.stem
    # Control: a version-2 message without those shapes, relabelled, is a
    # valid version-1 message: nothing else about it changed.
    unchanged = [p for p in VALID if not uses_v2_shapes(case(p)["message"])]
    assert {p.stem for p in unchanged} >= {"snapshot", "delta", "subscribe", "health"}
    for path in unchanged:
        test = case(path)
        assert v1_valid(test["entry"], relabel(test["message"], "eye.wire/1")), path.stem


def test_v2_validator_refuses_v1_messages_and_accepts_them_relabelled():
    for path in V1_VALID:
        test = case(path)
        with pytest.raises(WireValidationError):
            validate_message(json.dumps(test["message"]), test["entry"])
        assert wire_version_of(json.dumps(test["message"])) == "eye.wire/1"
        # Control: every version-1 shape is still a version-2 shape.
        validate_message(json.dumps(relabel(test["message"], SCHEMA_VERSION)), test["entry"])


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
