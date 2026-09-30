"""News and media items: the source-independent part of Package 4d.

A media item is one version of one article, image or video, as its source
published it. EYE keeps **metadata and a link only**: never an article body,
an image or a video. Any future adapter maps its provider's shape to the
fields below; ids, receipts, versions, corrections and display are the same
for every source.

Rules enforced here, again by migration 0006, and again by the wire schema:

* Each item is its own evidence, kept apart from event claims. Nothing here
  creates, confirms or links an event.
* Headlines, bylines, publishers and URLs are **untrusted input**: bounded,
  refused if they carry control or bidirectional-override characters, stored
  as given and only ever rendered as text. No model reads them on the way in.
* Times are kept apart: the source's first publication and this version's
  revision time come from the source; EYE's receipt time is stamped by EYE.
  A capture time for an image or video is the creator's **claim**, unverified.
* Reuse rights are per item. ``licensed`` needs a licence identifier and the
  attribution text; ``link_only`` means the source allows a link and metadata
  but no reuse of the work; ``unknown`` is shown as a link only, with its
  headline withheld.
* A location states its role (the event's place, the publisher's location or
  a place merely mentioned) and its method (stated by the source, or an
  automated geocode). Only a source-stated event place can be drawn.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

KINDS = frozenset({"article", "image", "video"})
STATUSES = frozenset({"published", "updated", "corrected", "retracted"})
RIGHTS = frozenset({"licensed", "link_only", "unknown"})
PLACE_ROLES = frozenset({"event_place", "publisher_location", "mentioned"})
PLACE_METHODS = frozenset({"source_stated", "automated_geocode"})
ITEM_FIELDS = frozenset(
    {
        "item_id",
        "kind",
        "status",
        "first_published_time",
        "revision_time",
        "url",
        "syndicated_from",
        "publisher",
        "creator",
        "headline",
        "language",
        "rights",
        "capture_time_claimed",
        "place",
    }
)
# Fields a provider might send that EYE never stores: bodies, media bytes,
# thumbnails. Named so the refusal says why.
CONTENT_FIELDS = frozenset(
    {"body", "content", "text", "html", "image", "video", "media", "thumbnail", "data"}
)
MAX_URL = 2000
MAX_HEADLINE = 300
MAX_NAME = 200
MAX_ATTRIBUTION = 300
MAX_PRECISION_M = 1_000_000
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LICENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$")
# C0/C1 controls, and bidirectional overrides and isolates that can make
# untrusted text read differently from what it is.
UNSAFE_TEXT = re.compile("[\u0000-\u001f\u007f-\u009f‪-‮⁦-⁩]")


@dataclass(frozen=True)
class Place:
    role: str
    method: str
    lon: float
    lat: float
    precision_m: float

    def as_json(self) -> dict:
        return {
            "role": self.role,
            "method": self.method,
            "coords": [self.lon, self.lat],
            "precision_m": self.precision_m,
        }


@dataclass(frozen=True)
class Item:
    item_id: str
    kind: str
    status: str
    first_published_time: datetime
    revision_time: datetime
    url: str
    syndicated_from: str | None
    publisher: str
    creator: str | None
    headline: str | None
    language: str | None
    rights: str
    licence: str | None
    attribution: str | None
    capture_time_claimed: datetime | None
    place: Place | None

    def content(self, iso) -> dict:
        """Everything the source said in this version; EYE receipt time is excluded."""
        return {
            "kind": self.kind,
            "status": self.status,
            "first_published_time": iso(self.first_published_time),
            "revision_time": iso(self.revision_time),
            "url": self.url,
            "syndicated_from": self.syndicated_from,
            "publisher": self.publisher,
            "creator": self.creator,
            "headline": self.headline,
            "language": self.language,
            "rights": [self.rights, self.licence, self.attribution],
            "capture_time_claimed": None
            if self.capture_time_claimed is None
            else iso(self.capture_time_claimed),
            "place": None if self.place is None else self.place.as_json(),
        }

    def content_sha256(self, iso) -> str:
        canonical = json.dumps(
            self.content(iso), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _text(value: object, what: str, limit: int, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError(f"{what} must be text of 1..{limit} characters")
    if UNSAFE_TEXT.search(value):
        raise ValueError(f"{what} contains control or bidirectional-override characters")
    if value != value.strip():
        raise ValueError(f"{what} has leading or trailing whitespace")
    return value


def url(value: object, what: str) -> str:
    """An https link to the original: no credentials, no spaces, bounded."""
    text = _text(value, what, MAX_URL, required=True)
    assert text is not None
    if any(c.isspace() for c in text):
        raise ValueError(f"{what} contains whitespace")
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"{what} must be an https URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{what} must not carry credentials")
    return text


def _number(value: object, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{what} must be a number from {low} to {high}")
    return float(value)


def _place(raw: object, what: str) -> Place | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"role", "method", "coords", "precision_m"}:
        raise ValueError(f"{what} must have exactly role, method, coords and precision_m")
    if raw["role"] not in PLACE_ROLES:
        raise ValueError(f"{what}.role must be one of {sorted(PLACE_ROLES)}")
    if raw["method"] not in PLACE_METHODS:
        raise ValueError(f"{what}.method must be one of {sorted(PLACE_METHODS)}")
    coords = raw["coords"]
    if not isinstance(coords, list) or len(coords) != 2:
        raise ValueError(f"{what}.coords must be [lon, lat]")
    return Place(
        role=raw["role"],
        method=raw["method"],
        lon=_number(coords[0], f"{what} longitude", -180, 180),
        lat=_number(coords[1], f"{what} latitude", -90, 90),
        precision_m=_number(raw["precision_m"], f"{what}.precision_m", 1, MAX_PRECISION_M),
    )


def _rights(raw: object, what: str) -> tuple[str, str | None, str | None]:
    if not isinstance(raw, dict) or raw.get("status") not in RIGHTS:
        raise ValueError(f"{what}.status must be one of {sorted(RIGHTS)}")
    status = raw["status"]
    if status != "licensed":
        if set(raw) != {"status"}:
            raise ValueError(f"{what}: only a licensed item names a licence and attribution")
        return status, None, None
    if set(raw) != {"status", "licence", "attribution"}:
        raise ValueError(f"{what}: a licensed item needs exactly a licence and attribution")
    licence = raw["licence"]
    if not isinstance(licence, str) or not LICENCE.match(licence):
        raise ValueError(f"{what}.licence must be a licence identifier such as CC-BY-4.0")
    attribution = _text(raw["attribution"], f"{what}.attribution", MAX_ATTRIBUTION, required=True)
    return status, licence, attribution


def parse_item(raw: object, index: int, received: datetime, parse_time) -> Item:
    """One item version. ``parse_time`` is the capture module's strict parser."""
    what = f"item {index}"
    if not isinstance(raw, dict):
        raise ValueError(f"{what} is not an object")
    if "received_time" in raw:
        raise ValueError(f"{what} supplies received_time; receipt time is stamped by EYE")
    carried = set(raw) & CONTENT_FIELDS
    if carried:
        raise ValueError(
            f"{what} carries {sorted(carried)}; EYE stores metadata and a link only, "
            "never article text, images or video"
        )
    if set(raw) - ITEM_FIELDS:
        raise ValueError(f"{what} has unexpected fields {sorted(set(raw) - ITEM_FIELDS)}")
    item_id = raw.get("item_id")
    if not isinstance(item_id, str) or not IDENTIFIER.match(item_id):
        raise ValueError(f"{what} has an invalid item_id")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ValueError(f"{what}.kind must be one of {sorted(KINDS)}")
    status = raw.get("status")
    if status not in STATUSES:
        raise ValueError(f"{what}.status must be one of {sorted(STATUSES)}")
    first = parse_time(raw.get("first_published_time"), f"{what} first_published_time")
    revision = parse_time(raw.get("revision_time"), f"{what} revision_time")
    if revision < first:
        raise ValueError(f"{what} was revised before it was first published")
    if (status == "published") != (revision == first):
        raise ValueError(
            f"{what}: a first publication has revision_time equal to first_published_time, "
            "and an update, correction or retraction a later one"
        )
    if received < revision:
        raise ValueError(f"{what} was revised after EYE received the response")
    captured = raw.get("capture_time_claimed")
    if captured is not None:
        if kind == "article":
            raise ValueError(f"{what}: only an image or video has a capture time")
        captured = parse_time(captured, f"{what} capture_time_claimed")
        if captured > first:
            raise ValueError(f"{what} claims a capture after it was first published")
    language = raw.get("language")
    if language is not None and (not isinstance(language, str) or not LANGUAGE.match(language)):
        raise ValueError(f"{what}.language must be a language tag such as en or pt-BR")
    rights, licence, attribution = _rights(raw.get("rights"), f"{what}.rights")
    syndicated = raw.get("syndicated_from")
    return Item(
        item_id=item_id,
        kind=kind,
        status=status,
        first_published_time=first,
        revision_time=revision,
        url=url(raw.get("url"), f"{what}.url"),
        syndicated_from=None if syndicated is None else url(syndicated, f"{what}.syndicated_from"),
        publisher=_text(raw.get("publisher"), f"{what}.publisher", MAX_NAME, required=True),
        creator=_text(raw.get("creator"), f"{what}.creator", MAX_NAME, required=False),
        headline=_text(raw.get("headline"), f"{what}.headline", MAX_HEADLINE, required=False),
        language=language,
        rights=rights,
        licence=licence,
        attribution=attribution,
        capture_time_claimed=captured,
        place=_place(raw.get("place"), f"{what}.place"),
    )
