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
from eye.wire import SCHEMA_PATH, WireValidationError, load_schema, validate_message
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
    "nan-number": '{"schema_version":"eye.wire/1","kind":"error","status":NaN,"error":"x"}',
    "trailing-newline-in-timestamp": json.dumps(
        {
            "schema_version": "eye.wire/1",
            "kind": "resync_required",
            "reason": "gap",
            "last_cursor": "c-0001\n",
        }
    ),
}
RAW_VALID = {
    "health": '{"schema_version":"eye.wire/1","kind":"health","status":"ok",'
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
    assert not reference_valid(test["entry"], test["message"]), "reference accepted it"


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
    broken["$defs"]["Timestamp"]["format"] = "date-time"
    with pytest.raises(SchemaDefinitionError, match="unsupported keyword"):
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
