"""Synthetic AIS vessel adapter (source ``synthetic-ais``).

Maps invented AIS-style position reports into Package 1's observation record
contract. The provider payload is archived byte for byte inside the capture
envelope; this module only interprets it. No real AIS feed is read.

Provider message format ``synthetic-ais/1`` (all fields required unless noted):

* ``mmsi`` - synthetic vessel identifier (never a real MMSI; ``SYNV-`` prefix).
* ``position_time`` - when the position was measured (source event time).
* ``sent_time`` - when the source issued this report (orders corrections).
* ``lon``, ``lat`` - WGS 84 degrees.
* ``accuracy`` - ``"high"`` or ``"low"``; low-accuracy reports are stored with
  the ``low_position_accuracy`` flag and excluded from crossing geometry.

A message may not carry an EYE receipt time; EYE stamps that itself.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from eye.ingest.capture import Record

SOURCE_ID = "synthetic-ais"
MESSAGE_FORMAT = "synthetic-ais/1"
VESSEL_ID = re.compile(r"^SYNV-[0-9]{4,9}$")
ACCURACY_CONFIDENCE = {"high": 0.95, "low": 0.5}
LOW_ACCURACY_FLAG = "low_position_accuracy"
MESSAGE_FIELDS = frozenset({"mmsi", "position_time", "sent_time", "lon", "lat", "accuracy"})


def messages(provider_response: object) -> list:
    """Return the message list from a ``synthetic-ais/1`` provider response."""
    if not isinstance(provider_response, dict):
        raise ValueError("provider_response must be an object")
    if provider_response.get("format") != MESSAGE_FORMAT:
        raise ValueError(f"provider_response.format must be {MESSAGE_FORMAT}")
    found = provider_response.get("messages")
    if not isinstance(found, list):
        raise ValueError("provider_response.messages must be a list")
    return found


def parse_message(raw: object, index: int, received: datetime) -> Record:
    """Map one AIS message to a Record, or raise ValueError to reject it."""
    from eye.ingest.capture import Record, _number, parse_time

    if not isinstance(raw, dict):
        raise ValueError(f"message {index} is not an object")
    if "received_time" in raw:
        raise ValueError(
            f"message {index} supplies received_time; receipt time is stamped by EYE, "
            "never by the provider"
        )
    unknown = set(raw) - MESSAGE_FIELDS
    if unknown:
        raise ValueError(f"message {index} has unexpected fields {sorted(unknown)}")
    vessel = raw.get("mmsi")
    if not isinstance(vessel, str) or not VESSEL_ID.match(vessel):
        raise ValueError(f"message {index} mmsi must be a synthetic SYNV- identifier")
    accuracy = raw.get("accuracy")
    if accuracy not in ACCURACY_CONFIDENCE:
        raise ValueError(f"message {index} accuracy must be high or low")
    observed = parse_time(raw.get("position_time"), f"message {index} position_time")
    published = parse_time(raw.get("sent_time"), f"message {index} sent_time")
    if published < observed:
        raise ValueError(f"message {index} was sent before its position was measured")
    if received < published:
        raise ValueError(f"message {index} was sent after EYE received the response")
    return Record(
        source_record_id=vessel,
        observed_time=observed,
        source_published_time=published,
        lon=_number(raw.get("lon"), f"message {index} lon", -180, 180),
        lat=_number(raw.get("lat"), f"message {index} lat", -90, 90),
        alt_m=None,
        confidence=ACCURACY_CONFIDENCE[accuracy],
        quality_flags=(LOW_ACCURACY_FLAG,) if accuracy == "low" else (),
    )
