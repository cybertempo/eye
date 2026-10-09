"""Sentinel-2 Level-2A archive checks for the raster worker (Package 6).

Source: ``copernicus-sentinel-2-l2a``, a **disabled candidate** in
``docs/source-policy-register.md``. This module never opens a network
connection and holds no credential: it inspects an archive that is already on
local scratch, checks its names, members, metadata and dates, and returns a
record or refuses with a stable reason code. Downloading belongs to ingest
(``eye.ingest.copernicus``).

What is checked (each refusal raises ``ImageryRefused`` with a ``code``):

- the product name (mission, ``MSIL2A``, sensing time, baseline, orbit, tile,
  discriminator) and that it is the product that was asked for;
- the zip structure: member count, names (no absolute path, ``..``,
  backslash or member outside the product directory), no symbolic link, no
  encryption, only stored or deflated members, total and per-member size and
  compression ratio, and every member's CRC;
- the metadata files ``manifest.safe`` and ``MTD_MSIL2A.xml``: size cap, no
  DOCTYPE or entity declaration, required fields present exactly once;
- every file the manifest lists exists and matches its MD5 or SHA3-256;
- dates: sensing time agrees between name and metadata, product start is not
  after stop, generation is not before stop, nothing is in the future, and no
  sensing time precedes Level-2A availability.

Reads are bounded: metadata is read up to ``max_metadata_bytes`` and every
other member is streamed in ``chunk_bytes`` pieces, so memory use does not grow
with product size. Structure checks follow the published SAFE layout; their
fit with a real CDSE product is UNVERIFIED until one is measured privately.
"""

from __future__ import annotations

import hashlib
import re
import stat
import xml.etree.ElementTree as ET  # noqa: S405 - DOCTYPE/ENTITY refused before parsing
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SOURCE_ID = "copernicus-sentinel-2-l2a"
PRODUCT_TYPE = "S2MSI2A"
PROCESSING_LEVEL = "Level-2A"
# Register row: L2A pilot products from 28 March 2017. Nothing earlier is L2A.
L2A_FIRST_DAY = datetime(2017, 3, 28, tzinfo=UTC)
# Allowed clock difference between EYE and the provider before a time counts
# as being in the future.
FUTURE_TOLERANCE = timedelta(minutes=5)
MISSIONS = {"S2A": "Sentinel-2A", "S2B": "Sentinel-2B", "S2C": "Sentinel-2C"}
MANIFEST = "manifest.safe"
METADATA = "MTD_MSIL2A.xml"

# Mission, level, sensing start, baseline, relative orbit, MGRS tile (100 km
# square letters skip I and O), discriminator.
PRODUCT_NAME = re.compile(
    r"(?P<mission>S2[ABC])_MSIL2A_(?P<sensing>\d{8}T\d{6})_N(?P<baseline>\d{4})"
    r"_R(?P<orbit>\d{3})_T(?P<tile>\d{2}[C-HJ-NP-X][A-HJ-NP-Z]{2})"
    r"_(?P<discriminator>\d{8}T\d{6})\.SAFE"
)
PRODUCT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_METADATA_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?Z")
_DECLARATION = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_CHECKSUMS = {"MD5": "md5", "SHA3-256": "sha3_256"}
_MTD_FIELDS = (
    "PRODUCT_URI",
    "PRODUCT_TYPE",
    "PROCESSING_LEVEL",
    "PROCESSING_BASELINE",
    "PRODUCT_START_TIME",
    "PRODUCT_STOP_TIME",
    "GENERATION_TIME",
    "SPACECRAFT_NAME",
    "DATATAKE_SENSING_START",
)


class ImageryRefused(ValueError):
    """An imagery request, response or archive was refused. Nothing is admitted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ArchiveLimits:
    """Bounds for inspecting one archive (from ``[imagery]`` in the configuration)."""

    max_product_bytes: int
    max_archive_members: int
    max_uncompressed_bytes: int
    max_compression_ratio: int
    max_metadata_bytes: int
    chunk_bytes: int


@dataclass(frozen=True)
class ProductName:
    name: str
    mission: str
    sensing: datetime
    baseline: str
    relative_orbit: int
    tile: str
    discriminator: datetime


@dataclass(frozen=True)
class ProductRecord:
    """A checked Level-2A archive. Times are the provider's, never EYE's."""

    source_id: str
    product_id: str
    name: str
    mission: str
    tile: str
    relative_orbit: int
    processing_baseline: str
    sensing_start: datetime
    product_start: datetime
    product_stop: datetime
    generation_time: datetime
    archive_bytes: int
    archive_sha256: str
    members: int
    verified_files: int
    attribution: str

    def to_json(self) -> dict:
        def stamp(value: datetime) -> str:
            return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        return {
            "source_id": self.source_id,
            "product_id": self.product_id,
            "name": self.name,
            "mission": self.mission,
            "tile": self.tile,
            "relative_orbit": self.relative_orbit,
            "processing_baseline": self.processing_baseline,
            "sensing_start": stamp(self.sensing_start),
            "product_start": stamp(self.product_start),
            "product_stop": stamp(self.product_stop),
            "generation_time": stamp(self.generation_time),
            "archive_bytes": self.archive_bytes,
            "archive_sha256": self.archive_sha256,
            "members": self.members,
            "verified_files": self.verified_files,
            "attribution": self.attribution,
        }


def _name_time(text: str, what: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ImageryRefused("date_invalid", f"{what} {text!r} is not a valid UTC time") from exc


def parse_product_name(name: str) -> ProductName:
    """Parse a Level-2A SAFE product name, or refuse it."""
    if not isinstance(name, str):
        raise ImageryRefused("product_name", "product name must be a string")
    match = PRODUCT_NAME.fullmatch(name)
    if match is None:
        raise ImageryRefused("product_name", f"{name!r} is not a Sentinel-2 L2A SAFE name")
    baseline = match["baseline"]
    return ProductName(
        name=name,
        mission=match["mission"],
        sensing=_name_time(match["sensing"], "sensing time"),
        baseline=f"{baseline[:2]}.{baseline[2:]}",
        relative_orbit=int(match["orbit"]),
        tile=match["tile"],
        discriminator=_name_time(match["discriminator"], "discriminator"),
    )


def check_product_id(product_id: str) -> str:
    """A catalogue product id is a lower-case UUID; anything else is refused."""
    if not isinstance(product_id, str) or PRODUCT_ID.fullmatch(product_id) is None:
        raise ImageryRefused("product_id", "product id must be a lower-case UUID")
    return product_id


def parse_metadata_time(text: str, field: str) -> datetime:
    """Parse an ISO 8601 UTC time with a ``Z`` suffix; no other form is accepted."""
    match = _METADATA_TIME.fullmatch(text)
    if match is None:
        raise ImageryRefused("date_invalid", f"{field} {text!r} is not an ISO UTC time with Z")
    fraction = match[7] or ""
    micros = int((fraction[1:] + "000000")[:6]) if fraction else 0
    try:
        return datetime(*(int(match[i]) for i in range(1, 7)), microsecond=micros, tzinfo=UTC)
    except ValueError as exc:
        raise ImageryRefused("date_invalid", f"{field} {text!r} is not a real date") from exc


def attribution_text(sensing: datetime, *, modified: bool) -> str:
    """Attribution required by the Sentinel data legal notice (register row).

    The year is the year the data was sensed (UTC), not the download year.
    Any resampled, reprojected, tiled or composited output is ``modified``.
    """
    year = sensing.astimezone(UTC).year
    if modified:
        return f"Contains modified Copernicus Sentinel data {year}"
    return f"Copernicus Sentinel data {year}"


def freshness(record: ProductRecord, now: datetime, stale_after_hours: int) -> dict:
    """How old the data is, measured from sensing time, never from download time.

    A product older than ``stale_after_hours`` is ``stale``: a view must show
    its sensing date and must not present it as the current state.
    """
    age = now - record.sensing_start
    if age < -FUTURE_TOLERANCE:
        raise ImageryRefused("date_future", "sensing time is in the future")
    age_hours = max(age, timedelta(0)) / timedelta(hours=1)
    return {
        "sensing_start": record.to_json()["sensing_start"],
        "age_hours": round(age_hours, 3),
        "state": "stale" if age_hours > stale_after_hours else "current",
    }


def _member_name(info: zipfile.ZipInfo, top: str) -> None:
    name = info.filename
    parts = name.rstrip("/").split("/")
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or re.match(r"[A-Za-z]:", name)
        or any(part in ("", ".", "..") for part in parts)
        or parts[0] != top
    ):
        raise ImageryRefused("member_name", f"archive member {name!r} is outside {top}/")


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise ImageryRefused(
            "metadata_too_large", f"{info.filename} is {info.file_size} bytes; limit {limit}"
        )
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ImageryRefused("metadata_too_large", f"{info.filename} exceeds {limit} bytes")
    return data


def _parse_xml(data: bytes, what: str) -> ET.Element:
    # The standard parser expands internal entities, so a declaration could be
    # used to inflate memory. SAFE metadata needs none; refuse any.
    if _DECLARATION.search(data):
        raise ImageryRefused("xml_doctype", f"{what} contains a DOCTYPE or ENTITY declaration")
    try:
        return ET.fromstring(data)  # noqa: S314 - declarations refused above
    except ET.ParseError as exc:
        raise ImageryRefused("xml_malformed", f"{what} is not well-formed XML") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _single_fields(root: ET.Element, fields: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, list[str]] = {field: [] for field in fields}
    for element in root.iter():
        name = _local(element.tag)
        if name in found:
            found[name].append((element.text or "").strip())
    values: dict[str, str] = {}
    for field, texts in found.items():
        if not texts or not texts[0]:
            raise ImageryRefused("field_missing", f"{METADATA} has no {field}")
        if len(texts) > 1:
            raise ImageryRefused("field_repeated", f"{METADATA} has {field} more than once")
        values[field] = texts[0]
    return values


def _manifest_files(root: ET.Element) -> list[tuple[str, str, str]]:
    """(href, algorithm, expected hex digest) for each data object in the manifest."""
    entries: list[tuple[str, str, str]] = []
    for element in root.iter():
        if _local(element.tag) != "byteStream":
            continue
        locations = [c for c in element if _local(c.tag) == "fileLocation"]
        checksums = [c for c in element if _local(c.tag) == "checksum"]
        if len(locations) != 1 or len(checksums) != 1:
            raise ImageryRefused(
                "manifest_reference", "each manifest byteStream needs one fileLocation and checksum"
            )
        href = locations[0].get("href", "")
        algorithm = checksums[0].get("checksumName", "")
        digest = (checksums[0].text or "").strip().lower()
        if algorithm not in _CHECKSUMS:
            raise ImageryRefused(
                "checksum_algorithm", f"manifest checksum {algorithm!r} is not MD5 or SHA3-256"
            )
        if not re.fullmatch(r"[0-9a-f]{32,128}", digest):
            raise ImageryRefused(
                "manifest_reference", f"manifest checksum for {href!r} is malformed"
            )
        entries.append((href, algorithm, digest))
    if not entries:
        raise ImageryRefused("manifest_reference", f"{MANIFEST} lists no data files")
    return entries


def _resolve_href(href: str, top: str) -> str:
    path = href[2:] if href.startswith("./") else href
    parts = path.split("/")
    if not path or any(part in ("", ".", "..") for part in parts) or "\\" in path:
        raise ImageryRefused("manifest_reference", f"manifest href {href!r} is not a member path")
    return f"{top}/{path}"


def _hash_file(path: Path, chunk_bytes: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_archive(
    path: Path,
    *,
    product_id: str,
    expected_name: str,
    limits: ArchiveLimits,
    now: datetime,
) -> ProductRecord:
    """Check one downloaded Level-2A zip and return its record, or refuse it."""
    check_product_id(product_id)
    expected = parse_product_name(expected_name)
    size = path.stat().st_size
    if size > limits.max_product_bytes:
        raise ImageryRefused(
            "product_too_large", f"archive is {size} bytes; limit {limits.max_product_bytes}"
        )
    if not zipfile.is_zipfile(path):
        raise ImageryRefused("not_a_zip", "the download is not a zip archive")
    try:
        with zipfile.ZipFile(path) as archive:
            record = _inspect(archive, product_id, expected, limits, now)
    except zipfile.BadZipFile as exc:
        raise ImageryRefused("archive_corrupt", f"zip archive is damaged: {exc}") from exc
    except (EOFError, OSError, NotImplementedError, RuntimeError) as exc:
        raise ImageryRefused("archive_corrupt", f"zip archive could not be read: {exc}") from exc
    return ProductRecord(
        **{**record, "archive_bytes": size, "archive_sha256": _hash_file(path, limits.chunk_bytes)}
    )


def _inspect(
    archive: zipfile.ZipFile,
    product_id: str,
    expected: ProductName,
    limits: ArchiveLimits,
    now: datetime,
) -> dict:
    top = expected.name
    infos = archive.infolist()
    if not infos:
        raise ImageryRefused("missing_metadata", "archive is empty")
    if len(infos) > limits.max_archive_members:
        raise ImageryRefused(
            "member_count", f"{len(infos)} members; limit {limits.max_archive_members}"
        )
    seen: set[str] = set()
    total = 0
    for info in infos:
        _member_name(info, top)
        key = info.filename.rstrip("/").lower()
        if key in seen:
            raise ImageryRefused("member_name", f"duplicate archive member {info.filename!r}")
        seen.add(key)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ImageryRefused("member_type", f"{info.filename!r} is not a regular file")
        if info.flag_bits & 0x1:
            raise ImageryRefused("encrypted", f"{info.filename!r} is encrypted")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ImageryRefused(
                "compression_method", f"{info.filename!r} uses compression {info.compress_type}"
            )
        if info.is_dir() and info.file_size:
            raise ImageryRefused("member_type", f"directory {info.filename!r} has content")
        total += info.file_size
        if total > limits.max_uncompressed_bytes:
            raise ImageryRefused(
                "uncompressed_too_large",
                f"archive expands beyond {limits.max_uncompressed_bytes} bytes",
            )
        if info.file_size > limits.max_compression_ratio * max(info.compress_size, 1):
            raise ImageryRefused(
                "compression_ratio",
                f"{info.filename!r} expands more than {limits.max_compression_ratio} times",
            )

    by_name = {info.filename: info for info in infos if not info.is_dir()}
    manifest_info = by_name.get(f"{top}/{MANIFEST}")
    metadata_info = by_name.get(f"{top}/{METADATA}")
    if manifest_info is None or metadata_info is None:
        raise ImageryRefused("missing_metadata", f"archive needs {MANIFEST} and {METADATA}")
    manifest = _parse_xml(
        _read_bounded(archive, manifest_info, limits.max_metadata_bytes), MANIFEST
    )
    metadata = _parse_xml(
        _read_bounded(archive, metadata_info, limits.max_metadata_bytes), METADATA
    )
    fields = _single_fields(metadata, _MTD_FIELDS)
    dates = _check_metadata(fields, expected, now)

    expected_digests: dict[str, tuple[str, str]] = {}
    for href, algorithm, digest in _manifest_files(manifest):
        member = _resolve_href(href, top)
        if member not in by_name:
            raise ImageryRefused("manifest_reference", f"manifest lists missing file {href!r}")
        if member in expected_digests:
            raise ImageryRefused("manifest_reference", f"manifest lists {href!r} twice")
        expected_digests[member] = (algorithm, digest)

    # One streaming pass over every file: zipfile checks each CRC at the end of
    # the member, and listed files are hashed against the manifest.
    for name, info in by_name.items():
        listed = expected_digests.get(name)
        digest = hashlib.new(_CHECKSUMS[listed[0]], usedforsecurity=False) if listed else None
        with archive.open(info) as handle:
            while chunk := handle.read(limits.chunk_bytes):
                if digest is not None:
                    digest.update(chunk)
        if listed and digest is not None and digest.hexdigest() != listed[1]:
            raise ImageryRefused(
                "checksum_mismatch", f"{name} does not match its manifest checksum"
            )

    return {
        "source_id": SOURCE_ID,
        "product_id": product_id,
        "name": expected.name,
        "mission": expected.mission,
        "tile": expected.tile,
        "relative_orbit": expected.relative_orbit,
        "processing_baseline": expected.baseline,
        **dates,
        "members": len(infos),
        "verified_files": len(expected_digests),
        "attribution": attribution_text(dates["sensing_start"], modified=False),
    }


def _check_metadata(fields: dict[str, str], expected: ProductName, now: datetime) -> dict:
    if fields["PRODUCT_TYPE"] != PRODUCT_TYPE or fields["PROCESSING_LEVEL"] != PROCESSING_LEVEL:
        raise ImageryRefused(
            "wrong_product_type",
            f"product is {fields['PRODUCT_TYPE']}/{fields['PROCESSING_LEVEL']}, "
            f"not {PRODUCT_TYPE}/{PROCESSING_LEVEL}",
        )
    if fields["PRODUCT_URI"] != expected.name:
        raise ImageryRefused(
            "name_mismatch", f"metadata names {fields['PRODUCT_URI']!r}, not the requested product"
        )
    if fields["PROCESSING_BASELINE"] != expected.baseline:
        raise ImageryRefused(
            "baseline_mismatch",
            f"metadata baseline {fields['PROCESSING_BASELINE']} differs from name "
            f"{expected.baseline}",
        )
    if fields["SPACECRAFT_NAME"] != MISSIONS[expected.mission]:
        raise ImageryRefused(
            "mission_mismatch", f"metadata spacecraft {fields['SPACECRAFT_NAME']!r} differs"
        )
    sensing = parse_metadata_time(fields["DATATAKE_SENSING_START"], "DATATAKE_SENSING_START")
    start = parse_metadata_time(fields["PRODUCT_START_TIME"], "PRODUCT_START_TIME")
    stop = parse_metadata_time(fields["PRODUCT_STOP_TIME"], "PRODUCT_STOP_TIME")
    generated = parse_metadata_time(fields["GENERATION_TIME"], "GENERATION_TIME")
    if sensing.replace(microsecond=0) != expected.sensing:
        raise ImageryRefused(
            "name_mismatch", "metadata sensing start differs from the product name's sensing time"
        )
    if start > stop:
        raise ImageryRefused("date_order", "PRODUCT_START_TIME is after PRODUCT_STOP_TIME")
    if generated < stop:
        raise ImageryRefused("date_order", "GENERATION_TIME is before PRODUCT_STOP_TIME")
    if expected.discriminator < expected.sensing:
        raise ImageryRefused("date_order", "the name's discriminator precedes its sensing time")
    if min(sensing, start) < L2A_FIRST_DAY:
        raise ImageryRefused("date_before_l2a", "sensing time precedes Level-2A availability")
    latest = now + FUTURE_TOLERANCE
    if max(sensing, stop, generated, expected.discriminator) > latest:
        raise ImageryRefused("date_future", "a product time is later than the current time")
    return {
        "sensing_start": sensing,
        "product_start": start,
        "product_stop": stop,
        "generation_time": generated,
    }
