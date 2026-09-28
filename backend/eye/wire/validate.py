"""Runtime validator for the EYE wire schema.

Standard library only. It supports exactly the JSON Schema keywords listed in
``SUPPORTED_KEYWORDS``; a schema using anything else is refused when loaded, so
a keyword can never be silently ignored. ``web/src/wire-validate.ts`` implements
the same rules for the browser, and tests compare both with a reference
implementation over a shared corpus of valid and invalid messages.
"""

from __future__ import annotations

import json
import math
import re
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "eye-wire.v1.schema.json"
SCHEMA_VERSION = "eye.wire/1"
MAX_MESSAGE_BYTES = 1_048_576
ENTRY_POINTS = ("ServerMessage", "ClientMessage")
MAX_ERRORS = 20

SUPPORTED_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "$ref",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "const",
        "items",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "oneOf",
    }
)
TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


class WireValidationError(ValueError):
    """A message does not conform to the wire schema."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class SchemaDefinitionError(ValueError):
    """The schema file uses a construct the validators do not implement."""


def _check_schema_node(node: Any, path: str) -> None:
    if not isinstance(node, dict):
        raise SchemaDefinitionError(f"{path}: schema node must be an object")
    unknown = set(node) - SUPPORTED_KEYWORDS
    if unknown:
        raise SchemaDefinitionError(f"{path}: unsupported keyword(s) {sorted(unknown)}")
    if "additionalProperties" in node and node["additionalProperties"] is not False:
        raise SchemaDefinitionError(f"{path}: additionalProperties must be false")
    if (
        node.get("type", "object") == "object"
        and "properties" in node
        and node.get("additionalProperties") is not False
    ):
        raise SchemaDefinitionError(f"{path}: objects must set additionalProperties false")
    types = node.get("type")
    for name in [types] if isinstance(types, str) else types or []:
        if name not in TYPES:
            raise SchemaDefinitionError(f"{path}: unknown type {name!r}")
    ref = node.get("$ref")
    if ref is not None and not re.fullmatch(r"#/\$defs/[A-Za-z][A-Za-z0-9]*", ref):
        raise SchemaDefinitionError(f"{path}: only local #/$defs references are supported")
    for key in ("pattern",):
        if key in node:
            re.compile(node[key])
    for name, child in node.get("$defs", {}).items():
        _check_schema_node(child, f"{path}/$defs/{name}")
    for name, child in node.get("properties", {}).items():
        _check_schema_node(child, f"{path}/properties/{name}")
    if "items" in node:
        _check_schema_node(node["items"], f"{path}/items")
    for index, child in enumerate(node.get("oneOf", [])):
        _check_schema_node(child, f"{path}/oneOf/{index}")


def _json_type_matches(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if name == "integer":
        return isinstance(value, int) or value.is_integer()
    return True  # number


class WireSchema:
    """A loaded, checked wire schema."""

    def __init__(self, document: dict) -> None:
        _check_schema_node(document, "#")
        self.document = document
        self.defs: dict[str, dict] = document.get("$defs", {})
        for entry in ENTRY_POINTS:
            if entry not in self.defs:
                raise SchemaDefinitionError(f"missing entry point {entry}")
        # Resolve every reference once so a dangling one fails at load time.
        for name, node in self._walk(document):
            ref = node.get("$ref")
            if ref and ref.split("/")[-1] not in self.defs:
                raise SchemaDefinitionError(f"{name}: dangling reference {ref}")
        self._patterns: dict[str, re.Pattern[str]] = {}

    def _walk(self, node: dict, path: str = "#"):
        yield path, node
        for key in ("$defs", "properties"):
            for name, child in node.get(key, {}).items():
                yield from self._walk(child, f"{path}/{key}/{name}")
        if "items" in node:
            yield from self._walk(node["items"], f"{path}/items")
        for index, child in enumerate(node.get("oneOf", [])):
            yield from self._walk(child, f"{path}/oneOf/{index}")

    def _pattern_ok(self, pattern: str, value: str) -> bool:
        compiled = self._patterns.get(pattern)
        if compiled is None:
            compiled = self._patterns[pattern] = re.compile(pattern)
        match = compiled.search(value)
        if match is None:
            return False
        # Python's "$" also matches before a final newline; JavaScript's does not.
        return not pattern.endswith("$") or match.end() == len(value)

    def errors(self, instance: Any, entry: str) -> list[str]:
        if entry not in ENTRY_POINTS:
            raise ValueError(f"unknown entry point {entry!r}")
        errors: list[str] = []
        self._check(instance, self.defs[entry], "$", errors)
        return errors[:MAX_ERRORS]

    def _check(self, value: Any, node: dict, path: str, errors: list[str]) -> None:
        if len(errors) >= MAX_ERRORS:
            return
        if "$ref" in node:
            self._check(value, self.defs[node["$ref"].split("/")[-1]], path, errors)
        if "oneOf" in node:
            matches = sum(1 for option in node["oneOf"] if not self._sub_errors(value, option))
            if matches != 1:
                errors.append(f"{path}: must match exactly one allowed form (matched {matches})")
                return
        if "const" in node and not _same(value, node["const"]):
            errors.append(f"{path}: must equal {node['const']!r}")
        if "enum" in node and not any(_same(value, option) for option in node["enum"]):
            errors.append(f"{path}: must be one of {node['enum']!r}")
        if "type" in node:
            names = [node["type"]] if isinstance(node["type"], str) else node["type"]
            if not any(_json_type_matches(value, name) for name in names):
                errors.append(f"{path}: must be of type {' or '.join(names)}")
                return
        if isinstance(value, str):
            if "minLength" in node and len(value) < node["minLength"]:
                errors.append(f"{path}: shorter than {node['minLength']}")
            if "maxLength" in node and len(value) > node["maxLength"]:
                errors.append(f"{path}: longer than {node['maxLength']}")
            if "pattern" in node and not self._pattern_ok(node["pattern"], value):
                errors.append(f"{path}: does not match the required format")
        if _json_type_matches(value, "number"):
            if "minimum" in node and value < node["minimum"]:
                errors.append(f"{path}: below minimum {node['minimum']}")
            if "maximum" in node and value > node["maximum"]:
                errors.append(f"{path}: above maximum {node['maximum']}")
        if isinstance(value, list):
            if "minItems" in node and len(value) < node["minItems"]:
                errors.append(f"{path}: fewer than {node['minItems']} items")
            if "maxItems" in node and len(value) > node["maxItems"]:
                errors.append(f"{path}: more than {node['maxItems']} items")
                return
            if "items" in node:
                for index, item in enumerate(value):
                    self._check(item, node["items"], f"{path}[{index}]", errors)
        if isinstance(value, dict):
            properties = node.get("properties", {})
            for name in node.get("required", []):
                if name not in value:
                    errors.append(f"{path}: missing required field {name!r}")
            if node.get("additionalProperties") is False:
                for name in value:
                    if name not in properties:
                        errors.append(f"{path}: unexpected field {name!r}")
            for name, child in properties.items():
                if name in value:
                    self._check(value[name], child, f"{path}.{name}", errors)

    def _sub_errors(self, value: Any, node: dict) -> list[str]:
        errors: list[str] = []
        self._check(value, node, "$", errors)
        return errors

    def validate(self, instance: Any, entry: str) -> Any:
        errors = self.errors(instance, entry)
        if errors:
            raise WireValidationError(errors)
        return instance


def _same(left: Any, right: Any) -> bool:
    # JSON equality: booleans are not numbers.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not valid JSON")


@cache
def load_schema(path: Path = SCHEMA_PATH) -> WireSchema:
    return WireSchema(json.loads(path.read_text(encoding="utf-8")))


def validate_message(
    payload: bytes | str | dict,
    entry: str,
    *,
    max_bytes: int = MAX_MESSAGE_BYTES,
    schema: WireSchema | None = None,
) -> dict:
    """Parse (if needed) and validate one message; raise WireValidationError if invalid."""
    if isinstance(payload, (bytes, str)):
        size = len(payload.encode("utf-8") if isinstance(payload, str) else payload)
        if size > max_bytes:
            raise WireValidationError([f"$: message is {size} bytes, limit {max_bytes}"])
        try:
            payload = json.loads(payload, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            raise WireValidationError([f"$: not valid JSON ({exc})"]) from exc
    return (schema or load_schema()).validate(payload, entry)
