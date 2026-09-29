"""Versioned wire protocol: schema loading and runtime validation.

The schema file ``schemas/eye-wire.v2.schema.json`` is the single source of
truth. This module interprets it with a deliberately small keyword subset; the
browser runs an equivalent interpreter generated from the same file.
"""

from eye.wire.validate import (
    MAX_MESSAGE_BYTES,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    V1_SCHEMA_PATH,
    WireSchema,
    WireValidationError,
    load_schema,
    validate_message,
    wire_version_of,
)

__all__ = [
    "MAX_MESSAGE_BYTES",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "V1_SCHEMA_PATH",
    "WireSchema",
    "WireValidationError",
    "load_schema",
    "validate_message",
    "wire_version_of",
]
