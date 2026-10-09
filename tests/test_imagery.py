"""Package 6: Copernicus Sentinel-2 L2A adapter on synthetic archives only.

No test here opens a network connection: downloads go through
``SyntheticTransport``, which answers from a fixed table. Every negative
control has a positive control next to it.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from eye.config import ConfigError, load_config
from eye.ingest import copernicus
from eye.ingest.copernicus import (
    DownloadLimits,
    acquire_product,
    check_destination,
    limits_from_config,
    transport_from_config,
)
from eye.raster.scratch import ScratchLimits, ScratchStore
from eye.raster.sentinel2 import (
    ArchiveLimits,
    ImageryRefused,
    attribution_text,
    freshness,
    inspect_archive,
    parse_product_name,
)
from synthetic_safe import (
    JP2_SIGNATURE,
    NAME,
    PRODUCT_ID,
    TOKEN,
    SyntheticResponse,
    SyntheticTransport,
    build_safe,
    data_files,
    download_url,
    image_files,
    manifest_xml,
    mtd_xml,
    product_name,
    safe_members,
    write_zip,
)

NOW = datetime(2026, 2, 20, 0, 0, tzinfo=UTC)
ARCHIVE_LIMITS = ArchiveLimits(
    max_product_bytes=1_000_000,
    max_archive_members=50,
    max_uncompressed_bytes=2_000_000,
    max_compression_ratio=100,
    max_metadata_bytes=64_000,
    chunk_bytes=4096,
)
DOWNLOAD = DownloadLimits(max_product_bytes=1_000_000, max_redirects=3, chunk_bytes=4096)
SCRATCH = ScratchLimits(
    max_scratch_bytes=4_000_000,
    max_cached_products=2,
    max_products_per_day=4,
    max_bytes_per_day=6_000_000,
)
HOST = "https://download.dataspace.copernicus.eu"


def noise(seed: str, size: int) -> bytes:
    """Deterministic bytes that do not compress (so size limits are really reached)."""
    return hashlib.shake_256(seed.encode()).digest(size)


def pid(n: int) -> str:
    return f"0e7e5000-0000-4000-8000-{n:012d}"


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def archive_bytes(tmp_path: Path) -> bytes:
    # Stored, with 20 kB placeholder bands, so the archive outweighs its record.json.
    members = safe_members(files=data_files(band_bytes=20_000))
    return write_zip(tmp_path / "valid.zip", members, zipfile.ZIP_STORED).read_bytes()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def scratch_root(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def store_for(root: Path, clock: Clock, limits: ScratchLimits = SCRATCH) -> ScratchStore:
    return ScratchStore(root, limits, clock)


def acquire(store, transport, *, product_id=PRODUCT_ID, name=NAME, download=DOWNLOAD, clock=None):
    return acquire_product(
        product_id,
        name,
        store=store,
        transport=transport,
        token_source=lambda: TOKEN,
        download=download,
        archive=ARCHIVE_LIMITS,
        now=clock or Clock(),
    )


def inspect(path: Path, *, name: str = NAME, limits: ArchiveLimits = ARCHIVE_LIMITS, now=NOW):
    return inspect_archive(path, product_id=PRODUCT_ID, expected_name=name, limits=limits, now=now)


def refused(code: str):
    return pytest.raises(ImageryRefused, match=rf"^{code}: ")


def partial_files(root: Path) -> list[Path]:
    return list((root / "partial").iterdir())


# -- known product and date result ---------------------------------------------


def test_known_product_gives_exact_record(tmp_path: Path) -> None:
    record = inspect(build_safe(tmp_path / "p.zip"))
    assert record.source_id == "copernicus-sentinel-2-l2a"
    assert record.name == NAME
    assert (record.mission, record.tile, record.relative_orbit) == ("S2B", "31NAA", 108)
    assert record.processing_baseline == "05.11"
    assert record.sensing_start == datetime(2026, 2, 14, 10, 30, 29, 24000, tzinfo=UTC)
    assert record.product_stop == datetime(2026, 2, 14, 10, 30, 29, 24000, tzinfo=UTC)
    assert record.generation_time == datetime(2026, 2, 14, 12, 55, 12, tzinfo=UTC)
    assert record.attribution == "Copernicus Sentinel data 2026"
    assert record.verified_files == 6 and record.members == 8
    assert record.to_json()["sensing_start"] == "2026-02-14T10:30:29.024000Z"


def test_freshness_counts_from_sensing_time_not_download(tmp_path: Path) -> None:
    record = inspect(build_safe(tmp_path / "p.zip"))
    sensing = record.sensing_start
    at_limit = freshness(record, sensing + timedelta(hours=240), stale_after_hours=240)
    past_limit = freshness(record, sensing + timedelta(hours=240, seconds=1), 240)
    assert at_limit["state"] == "current" and at_limit["age_hours"] == 240
    assert past_limit["state"] == "stale"
    assert at_limit["sensing_start"] == "2026-02-14T10:30:29.024000Z"
    with refused("date_future"):
        freshness(record, sensing - timedelta(minutes=6), 240)


def test_attribution_uses_sensing_year_and_marks_modified_output() -> None:
    late = datetime(2025, 12, 31, 23, 59, 59, 500000, tzinfo=UTC)
    assert attribution_text(late, modified=False) == "Copernicus Sentinel data 2025"
    assert (
        attribution_text(late, modified=True) == "Contains modified Copernicus Sentinel data 2025"
    )
    assert attribution_text(late + timedelta(seconds=1), modified=False).endswith("2026")


def test_attribution_year_comes_from_the_product_not_the_clock(tmp_path: Path) -> None:
    name = product_name(sensing="20251231T235959", discriminator="20260101T020000")
    fields = {
        "DATATAKE_SENSING_START": "2025-12-31T23:59:59.500Z",
        "PRODUCT_START_TIME": "2025-12-31T23:59:59.500Z",
        "PRODUCT_STOP_TIME": "2025-12-31T23:59:59.900Z",
        "GENERATION_TIME": "2026-01-01T02:00:00Z",
    }
    record = inspect(build_safe(tmp_path / "p.zip", name=name, fields=fields), name=name)
    assert record.attribution == "Copernicus Sentinel data 2025"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("DATATAKE_SENSING_START", "2026-02-30T10:30:29Z"),
        ("PRODUCT_START_TIME", "2026-02-14T10:30:29+00:00"),
        ("PRODUCT_STOP_TIME", "2026-02-14T10:30:29"),
        ("GENERATION_TIME", "2026-02-14 12:55:12Z"),
        ("GENERATION_TIME", "2026-02-14T24:00:00Z"),
    ],
)
def test_malformed_metadata_date_is_refused(tmp_path: Path, field: str, value: str) -> None:
    with refused("date_invalid"):
        inspect(build_safe(tmp_path / "p.zip", fields={field: value}))


def test_fractional_second_variants_are_accepted(tmp_path: Path) -> None:
    fields = {"GENERATION_TIME": "2026-02-14T12:55:12.5Z", "PRODUCT_STOP_TIME": "2026-02-14T10:31Z"}
    with refused("date_invalid"):
        inspect(build_safe(tmp_path / "bad.zip", fields=fields))
    fields["PRODUCT_STOP_TIME"] = "2026-02-14T10:31:00.123456Z"
    record = inspect(build_safe(tmp_path / "ok.zip", fields=fields))
    assert record.generation_time.microsecond == 500000


def test_invalid_calendar_date_in_name_is_refused(tmp_path: Path) -> None:
    with refused("date_invalid"):
        parse_product_name(product_name(sensing="20250229T103029"))
    leap = parse_product_name(product_name(sensing="20240229T103029"))
    assert leap.sensing == datetime(2024, 2, 29, 10, 30, 29, tzinfo=UTC)


def test_name_and_metadata_sensing_time_must_agree(tmp_path: Path) -> None:
    with refused("name_mismatch"):
        inspect(
            build_safe(
                tmp_path / "p.zip", fields={"DATATAKE_SENSING_START": "2026-02-14T10:30:30Z"}
            )
        )
    inspect(
        build_safe(
            tmp_path / "ok.zip", fields={"DATATAKE_SENSING_START": "2026-02-14T10:30:29.999Z"}
        )
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"PRODUCT_START_TIME": "2026-02-14T10:30:30Z"},
        {"GENERATION_TIME": "2026-02-14T10:30:29.000Z"},
    ],
)
def test_date_order_is_enforced(tmp_path: Path, fields: dict) -> None:
    with refused("date_order"):
        inspect(build_safe(tmp_path / "p.zip", fields=fields))


def test_discriminator_may_precede_sensing_but_not_be_in_the_future(tmp_path: Path) -> None:
    # ESA: the discriminator "can be earlier or slightly later" than sensing.
    name = product_name(discriminator="20260214T103028")
    assert inspect(build_safe(tmp_path / "ok.zip", name=name), name=name).name == name
    name = product_name(discriminator="20260220T001000")
    with refused("date_future"):
        inspect(build_safe(tmp_path / "p.zip", name=name), name=name)


def _dated(day: str) -> tuple[str, dict]:
    name = product_name(sensing=f"{day}T103029", discriminator=f"{day}T125512")
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    fields = {
        "DATATAKE_SENSING_START": f"{iso}T10:30:29Z",
        "PRODUCT_START_TIME": f"{iso}T10:30:29Z",
        "PRODUCT_STOP_TIME": f"{iso}T10:30:29Z",
        "GENERATION_TIME": f"{iso}T12:55:12Z",
    }
    return name, fields


def test_sensing_before_level_2a_availability_is_refused(tmp_path: Path) -> None:
    name, fields = _dated("20170327")
    with refused("date_before_l2a"):
        inspect(build_safe(tmp_path / "p.zip", name=name, fields=fields), name=name)
    name, fields = _dated("20170328")
    assert inspect(build_safe(tmp_path / "ok.zip", name=name, fields=fields), name=name)


def test_product_times_in_the_future_are_refused(tmp_path: Path) -> None:
    path = build_safe(tmp_path / "p.zip")
    generated = datetime(2026, 2, 14, 12, 55, 12, tzinfo=UTC)
    assert inspect(path, now=generated - timedelta(minutes=5))
    with refused("date_future"):
        inspect(path, now=generated - timedelta(minutes=5, seconds=1))


# -- malformed archives ----------------------------------------------------------


def test_valid_archive_is_the_positive_control(tmp_path: Path) -> None:
    assert inspect(build_safe(tmp_path / "p.zip")).name == NAME


def test_not_a_zip_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "p.zip"
    path.write_bytes(b"<html>login page</html>")
    with refused("not_a_zip"):
        inspect(path)


def test_truncated_zip_is_refused(tmp_path: Path, archive_bytes: bytes) -> None:
    path = tmp_path / "p.zip"
    path.write_bytes(archive_bytes[: len(archive_bytes) // 2])
    with pytest.raises(ImageryRefused) as caught:
        inspect(path)
    assert caught.value.code in ("not_a_zip", "archive_corrupt")


def test_damaged_member_data_fails_its_crc(tmp_path: Path) -> None:
    members = safe_members()
    path = write_zip(tmp_path / "p.zip", members, compression=zipfile.ZIP_STORED)
    data = bytearray(path.read_bytes())
    band = members[f"{NAME}/{next(iter(data_files()))}"]
    at = bytes(data).index(band)
    data[at] ^= 0xFF
    path.write_bytes(bytes(data))
    with refused("archive_corrupt"):
        inspect(path)


def test_checksum_mismatch_is_refused(tmp_path: Path) -> None:
    files = data_files()
    members = safe_members(files=files)
    first = next(iter(files))
    members[f"{NAME}/manifest.safe"] = manifest_xml(files, digests={first: "0" * 32})
    with refused("checksum_mismatch"):
        inspect(write_zip(tmp_path / "p.zip", members))


def test_sha3_checksums_are_verified_too(tmp_path: Path) -> None:
    files = data_files()
    assert inspect(build_safe(tmp_path / "ok.zip", files=files, algorithm="SHA3-256"))
    members = safe_members(files=files)
    members[f"{NAME}/manifest.safe"] = manifest_xml(
        files, "SHA3-256", digests={next(iter(files)): "ab" * 32}
    )
    with refused("checksum_mismatch"):
        inspect(write_zip(tmp_path / "bad.zip", members))


def test_unknown_checksum_algorithm_is_refused(tmp_path: Path) -> None:
    with refused("checksum_algorithm"):
        inspect(build_safe(tmp_path / "p.zip", algorithm="CRC32"))


def test_manifest_listing_a_missing_file_is_refused(tmp_path: Path) -> None:
    files = data_files()
    members = safe_members(files=files)
    del members[f"{NAME}/{next(iter(files))}"]
    with refused("manifest_reference"):
        inspect(write_zip(tmp_path / "p.zip", members))


@pytest.mark.parametrize(
    "member",
    [
        f"{NAME}/../escape.txt",
        "/etc/escape.txt",
        "S2A_MSIL2A_20260214T103029_N0511_R108_T31NAA_20260214T125512.SAFE/x.txt",
        f"{NAME}\\escape.txt",
        f"{NAME}/./x.txt",
        "C:/escape.txt",
    ],
)
def test_member_outside_product_directory_is_refused(tmp_path: Path, member: str) -> None:
    members = list(safe_members().items()) + [(member, b"x")]
    with refused("member_name"):
        inspect(write_zip(tmp_path / "p.zip", members))


def test_duplicate_member_differing_only_in_case_is_refused(tmp_path: Path) -> None:
    members = list(safe_members().items()) + [(f"{NAME}/mtd_msil2a.XML", b"x")]
    with refused("member_name"):
        inspect(write_zip(tmp_path / "p.zip", members))


def test_archive_for_another_product_is_refused(tmp_path: Path) -> None:
    other = product_name(tile="T31NAB")
    with refused("member_name"):
        inspect(build_safe(tmp_path / "p.zip", name=other))
    assert inspect(build_safe(tmp_path / "ok.zip", name=other), name=other)


def test_symbolic_link_member_is_refused(tmp_path: Path) -> None:
    link = zipfile.ZipInfo(f"{NAME}/link.jp2")
    link.external_attr = (0o120777 << 16) | 0
    members = list(safe_members().items()) + [(link, b"/etc/passwd")]
    with refused("member_type"):
        inspect(write_zip(tmp_path / "p.zip", members))
    plain = zipfile.ZipInfo(f"{NAME}/plain.txt")
    plain.external_attr = 0o100644 << 16
    assert inspect(write_zip(tmp_path / "ok.zip", list(safe_members().items()) + [(plain, b"x")]))


def _set_encrypted_flag(path: Path, member: str) -> None:
    # zipfile clears flag bits when writing, so set bit 0 in the local and
    # central headers of the written archive directly.
    data = bytearray(path.read_bytes())
    name = member.encode()
    at = data.find(name)
    while at != -1:
        if data[at - 30 : at - 26] == b"PK\x03\x04":
            data[at - 24] |= 0x1
        elif data[at - 46 : at - 42] == b"PK\x01\x02":
            data[at - 38] |= 0x1
        at = data.find(name, at + 1)
    path.write_bytes(bytes(data))


def test_encrypted_member_is_refused(tmp_path: Path) -> None:
    member = f"{NAME}/secret.bin"
    path = write_zip(tmp_path / "p.zip", list(safe_members().items()) + [(member, b"x")])
    assert inspect(path)
    _set_encrypted_flag(path, member)
    with zipfile.ZipFile(path) as archive:
        assert archive.getinfo(member).flag_bits & 0x1
    with refused("encrypted"):
        inspect(path)


def test_unsupported_compression_is_refused(tmp_path: Path) -> None:
    with refused("compression_method"):
        inspect(write_zip(tmp_path / "p.zip", safe_members(), compression=zipfile.ZIP_BZIP2))
    assert inspect(write_zip(tmp_path / "ok.zip", safe_members(), compression=zipfile.ZIP_STORED))


def test_member_count_limit(tmp_path: Path) -> None:
    # 5 required images and 2 metadata files, plus the extras.
    files = {f"extra/{i}.txt": b"x" for i in range(43)}
    assert inspect(build_safe(tmp_path / "ok.zip", files=files)).members == 50
    files = {f"extra/{i}.txt": b"x" for i in range(44)}
    with refused("member_count"):
        inspect(build_safe(tmp_path / "p.zip", files=files))


def test_uncompressed_size_limit(tmp_path: Path) -> None:
    limits = ArchiveLimits(**{**ARCHIVE_LIMITS.__dict__, "max_uncompressed_bytes": 300_000})
    big = {"band.jp2": noise("band", 256_000)}
    assert inspect(build_safe(tmp_path / "ok.zip", files=big), limits=limits)
    big["band2.jp2"] = noise("band2", 51_200)
    with refused("uncompressed_too_large"):
        inspect(build_safe(tmp_path / "p.zip", files=big), limits=limits)


def test_compression_ratio_limit_stops_a_zip_bomb(tmp_path: Path) -> None:
    with refused("compression_ratio"):
        inspect(build_safe(tmp_path / "p.zip", files={"zeros.jp2": bytes(1_000_000)}))
    limits = ArchiveLimits(**{**ARCHIVE_LIMITS.__dict__, "max_compression_ratio": 2000})
    assert inspect(
        build_safe(tmp_path / "ok.zip", files={"zeros.jp2": bytes(1_000_000)}), limits=limits
    )


def test_archive_larger_than_product_limit_is_refused(tmp_path: Path) -> None:
    path = build_safe(tmp_path / "p.zip")
    size = path.stat().st_size
    exact = ArchiveLimits(**{**ARCHIVE_LIMITS.__dict__, "max_product_bytes": size})
    assert inspect(path, limits=exact)
    under = ArchiveLimits(**{**ARCHIVE_LIMITS.__dict__, "max_product_bytes": size - 1})
    with refused("product_too_large"):
        inspect(path, limits=under)


@pytest.mark.parametrize("missing", ["manifest.safe", "MTD_MSIL2A.xml"])
def test_missing_metadata_file_is_refused(tmp_path: Path, missing: str) -> None:
    members = safe_members()
    del members[f"{NAME}/{missing}"]
    with refused("missing_metadata"):
        inspect(write_zip(tmp_path / "p.zip", members))


def test_empty_archive_is_refused(tmp_path: Path) -> None:
    with refused("missing_metadata"):
        inspect(write_zip(tmp_path / "p.zip", {}))


def test_metadata_size_limit(tmp_path: Path) -> None:
    members = safe_members()
    padding = "<Pad>" + noise("pad", 35_000).hex() + "</Pad>"
    members[f"{NAME}/MTD_MSIL2A.xml"] = mtd_xml({"PRODUCT_URI": NAME}, extra=padding)
    with refused("metadata_too_large"):
        inspect(write_zip(tmp_path / "p.zip", members))
    limits = ArchiveLimits(**{**ARCHIVE_LIMITS.__dict__, "max_metadata_bytes": 100_000})
    assert inspect(write_zip(tmp_path / "ok.zip", members), limits=limits)


def test_entity_expansion_is_refused_before_parsing(tmp_path: Path) -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><l>&b;</l>'
    )
    members = safe_members()
    members[f"{NAME}/manifest.safe"] = bomb
    with refused("xml_doctype"):
        inspect(write_zip(tmp_path / "p.zip", members))


def test_malformed_xml_is_refused(tmp_path: Path) -> None:
    members = safe_members()
    members[f"{NAME}/MTD_MSIL2A.xml"] = b"<Level-2A_User_Product><PRODUCT_URI>"
    with refused("xml_malformed"):
        inspect(write_zip(tmp_path / "p.zip", members))


def test_missing_and_repeated_fields_are_refused(tmp_path: Path) -> None:
    with refused("field_missing"):
        inspect(build_safe(tmp_path / "a.zip", fields={"GENERATION_TIME": None}))
    members = safe_members()
    members[f"{NAME}/MTD_MSIL2A.xml"] = mtd_xml(
        {"PRODUCT_URI": NAME}, extra="<PRODUCT_TYPE>S2MSI2A</PRODUCT_TYPE>"
    )
    with refused("field_repeated"):
        inspect(write_zip(tmp_path / "b.zip", members))


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"PRODUCT_TYPE": "S2MSI1C"}, "wrong_product_type"),
        ({"PROCESSING_LEVEL": "Level-1C"}, "wrong_product_type"),
        ({"PRODUCT_URI": product_name(tile="T31NAB")}, "name_mismatch"),
        ({"PROCESSING_BASELINE": "05.10"}, "baseline_mismatch"),
        ({"SPACECRAFT_NAME": "Sentinel-2A"}, "mission_mismatch"),
    ],
)
def test_metadata_must_match_the_requested_product(tmp_path: Path, fields: dict, code: str) -> None:
    with refused(code):
        inspect(build_safe(tmp_path / "p.zip", fields=fields))


@pytest.mark.parametrize(
    "name",
    [
        "S2B_MSIL1C_20260214T103029_N0511_R108_T31NAA_20260214T125512.SAFE",
        "S2B_MSIL2A_20260214T103029_N0511_R108_T31NIA_20260214T125512.SAFE",
        "S2D_MSIL2A_20260214T103029_N0511_R108_T31NAA_20260214T125512.SAFE",
        "S2B_MSIL2A_20260214T103029_N0511_R108_T31NAA_20260214T125512.SAFE/",
        "../S2B_MSIL2A_20260214T103029_N0511_R108_T31NAA_20260214T125512.SAFE",
    ],
)
def test_malformed_product_name_is_refused(name: str) -> None:
    with refused("product_name"):
        parse_product_name(name)
    assert parse_product_name(NAME).tile == "31NAA"


# -- destination and redirect rules ----------------------------------------------


def test_direct_download_sends_token_only_to_the_download_host(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    transport = SyntheticTransport({download_url(): SyntheticResponse(200, archive_bytes)})
    with store_for(scratch_root, clock) as store:
        result = acquire(store, transport)
    assert not result.from_cache and result.transferred_bytes == len(archive_bytes)
    assert result.product.record["sensing_start"] == "2026-02-14T10:30:29.024000Z"
    assert transport.requests == [(download_url(), {"Authorization": f"Bearer {TOKEN}"})]
    assert partial_files(scratch_root) == []


@pytest.mark.parametrize(
    "location",
    [
        f"{HOST}/odata/v1/Products({PRODUCT_ID})/$value?segment=1",
        "/storage/synthetic/product.zip",
        "storage/product.zip",
    ],
)
def test_redirect_on_the_download_host_is_followed(
    scratch_root: Path, clock: Clock, archive_bytes: bytes, location: str
) -> None:
    from urllib.parse import urljoin

    target = urljoin(download_url(), location)
    transport = SyntheticTransport(
        {
            download_url(): SyntheticResponse(302, headers={"Location": location}),
            target: SyntheticResponse(200, archive_bytes),
        }
    )
    with store_for(scratch_root, clock) as store:
        assert acquire(store, transport).product.product_id == PRODUCT_ID
    assert [url for url, _ in transport.requests] == [download_url(), target]
    assert all(h == {"Authorization": f"Bearer {TOKEN}"} for _, h in transport.requests)


UNLISTED = [
    "https://catalogue.dataspace.copernicus.eu/odata/v1/Products(x)/$value",
    "https://zipper.dataspace.copernicus.eu/odata/v1/Products(x)/$value",
    "https://identity.dataspace.copernicus.eu/auth",
    "https://evil.invalid/product.zip",
    "https://download.dataspace.copernicus.eu.evil.invalid/product.zip",
    "https://evil.invalid/download.dataspace.copernicus.eu/product.zip",
    "https://download.dataspace.copernicus.eu@evil.invalid/product.zip",
    "https://user:pw@download.dataspace.copernicus.eu/product.zip",
    "https://download.dataspace.copernicus.eu:8443/product.zip",
    "https://download.dataspace.copernicus.eu:443/product.zip",
    "https://DOWNLOAD.dataspace.copernicus.eu/product.zip",
    "https://download.dataspace.copernicus.eu./product.zip",
    "http://download.dataspace.copernicus.eu/product.zip",
    "ftp://download.dataspace.copernicus.eu/product.zip",
    "https://192.0.2.10/product.zip",
    "https://[2001:db8::1]/product.zip",
    "//evil.invalid/product.zip",
    "https://download.dataspace.copernicus.eu\\@evil.invalid/product.zip",
    "https://download.dataspace.copernicus.eu/product.zip#frag",
    "https://download.dataspace.copernicus.eu/pro duct.zip",
    "https://download.dataspace.copernicus.eu/product.zip\r\nX-Injected: 1",
]


@pytest.mark.parametrize("location", UNLISTED)
def test_redirect_to_unlisted_destination_is_refused_without_the_token(
    scratch_root: Path, clock: Clock, archive_bytes: bytes, location: str
) -> None:
    from urllib.parse import urljoin

    # The unlisted destination is routed, so a request to it would succeed if
    # the adapter sent one; the transport records that it never did.
    transport = SyntheticTransport(
        {
            download_url(): SyntheticResponse(307, headers={"Location": location}),
            urljoin(download_url(), location): SyntheticResponse(200, archive_bytes),
        }
    )
    with store_for(scratch_root, clock) as store:
        with refused("destination_refused") as caught:
            acquire(store, transport)
        assert store.cached() == []
    assert [url for url, _ in transport.requests] == [download_url()]
    assert TOKEN not in str(caught.value) and "pw" not in str(caught.value)
    assert partial_files(scratch_root) == []


@pytest.mark.parametrize("url", UNLISTED)
def test_check_destination_refuses_unlisted_urls(url: str) -> None:
    with refused("destination_refused"):
        check_destination(url)
    assert check_destination(download_url()) == download_url()


def test_redirect_limit(scratch_root: Path, clock: Clock, archive_bytes: bytes) -> None:
    def chain(hops: int, product_id: str = PRODUCT_ID) -> SyntheticTransport:
        urls = [download_url(product_id)] + [f"{HOST}/hop/{i}" for i in range(1, hops + 1)]
        routes = {
            urls[i]: SyntheticResponse(301, headers={"Location": urls[i + 1]}) for i in range(hops)
        }
        routes[urls[-1]] = SyntheticResponse(200, archive_bytes)
        return SyntheticTransport(routes)

    with store_for(scratch_root, clock) as store:
        within = chain(3)
        assert acquire(store, within).product.product_id == PRODUCT_ID
        assert len(within.requests) == 4
        beyond = chain(4, pid(2))
        with refused("redirect_limit"):
            acquire(store, beyond, product_id=pid(2))
        assert len(beyond.requests) == 4


def test_redirect_loop_and_missing_location_are_refused(scratch_root: Path, clock: Clock) -> None:
    loop = SyntheticTransport(
        {
            download_url(): SyntheticResponse(302, headers={"Location": f"{HOST}/a"}),
            f"{HOST}/a": SyntheticResponse(302, headers={"Location": download_url()}),
        }
    )
    no_location = SyntheticTransport({download_url(): SyntheticResponse(303)})
    with store_for(scratch_root, clock) as store:
        with refused("redirect_loop"):
            acquire(store, loop)
        with refused("redirect_without_location"):
            acquire(store, no_location)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "provider_auth_refused"),
        (403, "provider_auth_refused"),
        (404, "provider_not_found"),
        (429, "provider_unavailable"),
        (503, "provider_unavailable"),
        (204, "provider_status"),
        (300, "provider_status"),
    ],
)
def test_provider_status_is_refused_with_a_stable_code(
    scratch_root: Path, clock: Clock, status: int, code: str
) -> None:
    transport = SyntheticTransport({download_url(): SyntheticResponse(status, b"denied")})
    with store_for(scratch_root, clock) as store:
        with refused(code) as caught:
            acquire(store, transport)
        assert store.cached() == []
    assert TOKEN not in str(caught.value)
    assert transport.routes[download_url()].closed


def test_token_is_fetched_per_request_and_must_be_usable(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    tokens = iter(["first-token", "refreshed-token"])
    transport = SyntheticTransport(
        {
            download_url(): SyntheticResponse(302, headers={"Location": f"{HOST}/b"}),
            f"{HOST}/b": SyntheticResponse(200, archive_bytes),
        }
    )
    with store_for(scratch_root, clock) as store:
        acquire_product(
            PRODUCT_ID,
            NAME,
            store=store,
            transport=transport,
            token_source=lambda: next(tokens),
            download=DOWNLOAD,
            archive=ARCHIVE_LIMITS,
            now=clock,
        )
        bad = SyntheticTransport()
        for token in ("", "two\nlines", None):
            with refused("token_invalid"):
                acquire_product(
                    pid(2),
                    NAME,
                    store=store,
                    transport=bad,
                    token_source=lambda t=token: t,
                    download=DOWNLOAD,
                    archive=ARCHIVE_LIMITS,
                    now=clock,
                )
    assert [h["Authorization"] for _, h in transport.requests] == [
        "Bearer first-token",
        "Bearer refreshed-token",
    ]
    assert bad.requests == []


def test_malformed_product_id_is_refused_before_any_request(
    scratch_root: Path, clock: Clock
) -> None:
    transport = SyntheticTransport()
    with store_for(scratch_root, clock) as store:
        for bad in ("0E7E5000-0000-4000-8000-000000000001", "x)/$value?", "../1", ""):
            with refused("product_id"):
                acquire(store, transport, product_id=bad)
    assert transport.requests == []


# -- size, memory and scratch limits ---------------------------------------------


def test_declared_length_over_cap_is_refused_before_reading(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    exact = DownloadLimits(len(archive_bytes), 3, 4096)
    under = DownloadLimits(len(archive_bytes) - 1, 3, 4096)
    big = SyntheticResponse(200, archive_bytes)
    with store_for(scratch_root, clock) as store:
        with refused("product_too_large"):
            acquire(store, SyntheticTransport({download_url(): big}), download=under)
        assert big.read_sizes == []
        ok = SyntheticTransport({download_url(): SyntheticResponse(200, archive_bytes)})
        assert acquire(store, ok, download=exact).transferred_bytes == len(archive_bytes)


def test_undeclared_body_is_cut_off_at_the_cap(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    endless = SyntheticResponse(200, archive_bytes + bytes(200_000), content_length=False)
    limits = DownloadLimits(len(archive_bytes) + 10_000, 3, 4096)
    with store_for(scratch_root, clock) as store:
        with refused("product_too_large"):
            acquire(store, SyntheticTransport({download_url(): endless}), download=limits)
        assert endless.bytes_served == limits.max_product_bytes
        assert store.budget_today()["bytes"] == endless.bytes_served
        assert partial_files(scratch_root) == []
        exact = SyntheticResponse(200, archive_bytes, content_length=False)
        assert acquire(store, SyntheticTransport({download_url(): exact}), download=limits)


def test_reads_never_exceed_the_chunk_size(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    response = SyntheticResponse(200, archive_bytes)
    with store_for(scratch_root, clock) as store:
        acquire(store, SyntheticTransport({download_url(): response}))
    assert response.read_sizes and max(response.read_sizes) <= DOWNLOAD.chunk_bytes


def test_body_shorter_than_its_declared_length_is_refused(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    headers = {"Content-Length": str(len(archive_bytes) + 1)}
    response = SyntheticResponse(200, archive_bytes, headers)
    with store_for(scratch_root, clock) as store:
        with refused("length_mismatch"):
            acquire(store, SyntheticTransport({download_url(): response}))
        assert store.cached() == [] and partial_files(scratch_root) == []
    assert response.bytes_served == len(archive_bytes)


def test_declared_body_is_read_only_to_its_declared_length(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    # Bytes after the declared length are never read; the cut archive then
    # fails the archive check. The exact declared length is the control.
    short = SyntheticResponse(200, archive_bytes, {"Content-Length": str(len(archive_bytes) - 1)})
    exact = SyntheticResponse(200, archive_bytes)
    with store_for(scratch_root, clock) as store:
        with pytest.raises(ImageryRefused) as caught:
            acquire(store, SyntheticTransport({download_url(): short}))
        assert caught.value.code in ("not_a_zip", "archive_corrupt")
        assert short.bytes_served == len(archive_bytes) - 1
        assert acquire(store, SyntheticTransport({download_url(pid(2)): exact}), product_id=pid(2))
    assert exact.bytes_served == len(archive_bytes)


class FailingResponse(SyntheticResponse):
    def read(self, size: int) -> bytes:
        if self.read_sizes:
            raise ConnectionResetError("synthetic reset")
        return super().read(size)


class FailingTransport(SyntheticTransport):
    def get(self, url, headers):
        self.requests.append((url, dict(headers)))
        raise TimeoutError("synthetic timeout")


def test_broken_transfers_are_refused_and_counted(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    reset = FailingResponse(200, archive_bytes)
    with store_for(scratch_root, clock) as store:
        with refused("transfer_failed"):
            acquire(store, SyntheticTransport({download_url(): reset}))
        assert store.budget_today()["bytes"] == DOWNLOAD.chunk_bytes
        with refused("provider_unavailable"):
            acquire(store, FailingTransport(), product_id=pid(2))
        bad_length = SyntheticResponse(200, archive_bytes, {"Content-Length": "12x"})
        with refused("length_invalid"):
            acquire(
                store, SyntheticTransport({download_url(pid(3)): bad_length}), product_id=pid(3)
            )
        assert store.cached() == [] and partial_files(scratch_root) == []
        ok = SyntheticTransport({download_url(pid(4)): SyntheticResponse(200, archive_bytes)})
        assert acquire(store, ok, product_id=pid(4))


def test_malformed_download_is_refused_and_removed(scratch_root: Path, clock: Clock) -> None:
    page = SyntheticResponse(200, b"<html>please log in</html>")
    with store_for(scratch_root, clock) as store:
        with refused("not_a_zip"):
            acquire(store, SyntheticTransport({download_url(): page}))
        assert store.cached() == [] and partial_files(scratch_root) == []
        assert store.budget_today() == {"day": "2026-02-20", "products": 1, "bytes": 26}


def test_daily_product_cap_resets_next_utc_day(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    limits = ScratchLimits(10_000_000, 10, 2, 10_000_000)

    def ok() -> SyntheticTransport:
        return SyntheticTransport(
            {download_url(pid(n)): SyntheticResponse(200, archive_bytes) for n in range(1, 5)}
        )

    with store_for(scratch_root, clock, limits) as store:
        acquire(store, ok(), product_id=pid(1), clock=clock)
        acquire(store, ok(), product_id=pid(2), clock=clock)
        transport = ok()
        with refused("daily_product_cap"):
            acquire(store, transport, product_id=pid(3), clock=clock)
        assert transport.requests == []
        clock.advance(days=1)
        assert acquire(store, ok(), product_id=pid(3), clock=clock)


def test_daily_byte_cap_cuts_the_download(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    size = len(archive_bytes)
    limits = ScratchLimits(10_000_000, 10, 10, size + size // 2)
    routes = {download_url(pid(n)): SyntheticResponse(200, archive_bytes) for n in (1, 2, 3)}
    with store_for(scratch_root, clock, limits) as store:
        assert acquire(store, SyntheticTransport(routes), product_id=pid(1), clock=clock)
        with refused("daily_byte_cap"):
            acquire(store, SyntheticTransport(routes), product_id=pid(2), clock=clock)
        assert routes[download_url(pid(2))].read_sizes == []
        assert store.remaining_bytes_today() == size // 2
        clock.advance(days=1)
        assert acquire(store, SyntheticTransport(routes), product_id=pid(3), clock=clock)


def test_least_recently_used_product_is_evicted(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    routes = {download_url(pid(n)): SyntheticResponse(200, archive_bytes) for n in (1, 2, 3)}
    with store_for(scratch_root, clock) as store:
        first = acquire(store, SyntheticTransport(routes), product_id=pid(1), clock=clock)
        clock.advance(minutes=1)
        second = acquire(store, SyntheticTransport(routes), product_id=pid(2), clock=clock)
        assert first.evicted == () and second.evicted == ()
        clock.advance(minutes=1)
        again = acquire(store, SyntheticTransport(), product_id=pid(1), clock=clock)
        assert again.from_cache and again.transferred_bytes == 0
        clock.advance(minutes=1)
        third = acquire(store, SyntheticTransport(routes), product_id=pid(3), clock=clock)
        assert [item.product_id for item in third.evicted] == [pid(2)]
        assert third.evicted[0].record["attribution"] == "Copernicus Sentinel data 2026"
        assert sorted(item.product_id for item in store.cached()) == [pid(1), pid(3)]
    assert not (scratch_root / "products" / pid(2)).exists()


def test_eviction_by_bytes_and_refusal_without_eviction(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    size = len(archive_bytes)
    download = DownloadLimits(size, 3, 4096)
    # Room for two archives in bytes, any number by count.
    limits = ScratchLimits(2 * size + size // 2, 10, 10, 100 * size)
    routes = {download_url(pid(n)): SyntheticResponse(200, archive_bytes) for n in (1, 2, 3)}
    with store_for(scratch_root, clock, limits) as store:
        for n in (1, 2):
            acquire(
                store, SyntheticTransport(routes), product_id=pid(n), download=download, clock=clock
            )
            clock.advance(seconds=1)
        result = acquire(
            store, SyntheticTransport(routes), product_id=pid(3), download=download, clock=clock
        )
        assert [item.product_id for item in result.evicted] == [pid(1)]
        assert len(store.cached()) == 2
        # A product that cannot fit even in an empty scratch evicts nothing.
        too_big = DownloadLimits(limits.max_scratch_bytes + 1, 3, 4096)
        transport = SyntheticTransport()
        with refused("scratch_full"):
            acquire(store, transport, product_id=pid(4), download=too_big, clock=clock)
        assert transport.requests == []
        assert sorted(item.product_id for item in store.cached()) == [pid(2), pid(3)]


def test_scratch_lock_admits_one_process(scratch_root: Path, clock: Clock) -> None:
    with store_for(scratch_root, clock):
        second = store_for(scratch_root, clock)
        with refused("scratch_locked"):
            second.__enter__()
    with store_for(scratch_root, clock) as store:
        assert store.cached() == []


def test_leftover_partial_is_removed_on_open(scratch_root: Path, clock: Clock) -> None:
    (scratch_root / "partial").mkdir()
    (scratch_root / "partial" / f"{PRODUCT_ID}.zip").write_bytes(b"half")
    with store_for(scratch_root, clock) as store:
        assert partial_files(scratch_root) == [] and store.used_bytes() == 0


def test_missing_scratch_directory_is_refused(tmp_path: Path, clock: Clock) -> None:
    with refused("scratch_missing"), store_for(tmp_path / "absent", clock):
        pass


def test_damaged_scratch_state_stops_the_store(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    routes = {download_url(): SyntheticResponse(200, archive_bytes)}
    with store_for(scratch_root, clock) as store:
        acquire(store, SyntheticTransport(routes), clock=clock)
        assert len(store.cached()) == 1
        record = scratch_root / "products" / PRODUCT_ID / "record.json"
        record.write_text("{not json", encoding="utf-8")
        with refused("scratch_corrupt"):
            store.cached()
        record.unlink()
        (scratch_root / "products" / PRODUCT_ID / "product.zip").unlink()
        (scratch_root / "products" / PRODUCT_ID).rmdir()
        assert store.cached() == []
        budget = scratch_root / "budget.json"
        for bad in (
            '{"day": "2026-02-20", "products": -1, "bytes": 0}',
            "[]",
            "{",
            '{"day": "2026-02-21", "products": 0, "bytes": 0}',
        ):
            budget.write_text(bad, encoding="utf-8")
            with refused("scratch_corrupt"):
                store.budget_today()
        budget.write_text('{"day": "2026-02-19", "products": 9, "bytes": 9}', encoding="utf-8")
        assert store.budget_today() == {"day": "2026-02-20", "products": 0, "bytes": 0}


def test_record_json_holds_the_product_record(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    with store_for(scratch_root, clock) as store:
        acquire(store, SyntheticTransport({download_url(): SyntheticResponse(200, archive_bytes)}))
    saved = json.loads((scratch_root / "products" / PRODUCT_ID / "record.json").read_text())
    assert saved["record"]["name"] == NAME and saved["record"]["tile"] == "31NAA"
    assert saved["last_used"] == "2026-02-20T00:00:00.000000Z"
    assert TOKEN not in json.dumps(saved)


# -- configuration and the disabled source ---------------------------------------


def test_example_imagery_limits_are_explicit(example_raw: dict) -> None:
    imagery = example_raw["imagery"]
    assert imagery["enabled"] is False
    for key in (
        "max_product_bytes",
        "max_scratch_bytes",
        "max_cached_products",
        "max_products_per_day",
        "max_bytes_per_day",
        "max_redirects",
        "stale_after_hours",
    ):
        assert isinstance(imagery[key], int), key


def test_limits_come_from_configuration(write_config, example_raw: dict) -> None:
    example_raw["imagery"] = {
        **example_raw["imagery"],
        "max_product_bytes": 2048,
        "chunk_bytes": 4096,
    }
    config = load_config(write_config(example_raw))
    download, archive, scratch = limits_from_config(config.imagery)
    assert download == DownloadLimits(2048, 3, 4096)
    assert archive.max_product_bytes == 2048 and archive.chunk_bytes == 4096
    assert scratch == ScratchLimits(4_000_000_000, 2, 4, 6_000_000_000)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"enabled": True}, "imagery.enabled is refused"),
        ({"enabled": "no"}, "imagery.enabled must be true or false"),
        ({"max_scratch_bytes": 1024, "max_product_bytes": 2048}, "max_scratch_bytes"),
        ({"max_bytes_per_day": 1024, "max_product_bytes": 2048}, "max_bytes_per_day"),
        ({"max_redirects": 6}, "max_redirects"),
        ({"scratch_dir_env": "/tmp/scratch"}, "scratch_dir_env"),
        ({"download_host": "evil.invalid"}, "unknown key"),
    ],
)
def test_unsafe_imagery_configuration_is_refused(
    write_config, example_raw: dict, change: dict, message: str
) -> None:
    load_config(write_config(example_raw))
    example_raw["imagery"] = {**example_raw["imagery"], **change}
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(example_raw))


def test_source_cannot_be_enabled_as_a_provider(write_config, example_raw: dict) -> None:
    example_raw["providers"] = {"enabled": ["copernicus-sentinel-2-l2a"]}
    with pytest.raises(ConfigError, match="refusing: copernicus-sentinel-2-l2a"):
        load_config(write_config(example_raw))


def test_real_transport_is_always_refused(write_config, example_raw: dict) -> None:
    config = load_config(write_config(example_raw))
    with refused("source_disabled"):
        transport_from_config(config)


NETWORK_IMPORTS = (
    "import socket",
    "http.client",
    "urllib.request",
    "import ssl",
    "import requests",
)


def test_adapter_has_no_network_code() -> None:
    import eye.raster.scratch
    import eye.raster.sentinel2

    for module in (copernicus, eye.raster.sentinel2, eye.raster.scratch):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in NETWORK_IMPORTS:
            assert forbidden not in source, (module.__name__, forbidden)
    # Positive control: the same scan finds network code in a module that has it.
    import urllib.request

    source = Path(urllib.request.__file__).read_text(encoding="utf-8")
    assert any(forbidden in source for forbidden in NETWORK_IMPORTS)


# -- audit repairs: O76 (cache re-check), O77 (image structure), O78 (daily cap) --


def _flip_middle_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))


def test_o76_corrupted_cache_hit_is_refused_and_evicted(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    with store_for(scratch_root, clock) as store:
        acquire(store, SyntheticTransport({download_url(): SyntheticResponse(200, archive_bytes)}))
        # Control: an intact cache hit passes the re-check with no request.
        hit = acquire(store, SyntheticTransport())
        assert hit.from_cache and hit.transferred_bytes == 0
        _flip_middle_byte(store.archive_path(PRODUCT_ID))
        quiet = SyntheticTransport()
        with refused("cache_corrupt"):
            acquire(store, quiet)
        assert quiet.requests == [] and store.cached() == []
        # The next request downloads and checks the product again.
        again = acquire(
            store, SyntheticTransport({download_url(): SyntheticResponse(200, archive_bytes)})
        )
        assert not again.from_cache and again.product.record["name"] == NAME


def test_o76_cache_hit_must_match_its_stored_record(
    scratch_root: Path, clock: Clock, tmp_path: Path, archive_bytes: bytes
) -> None:
    # A different but valid archive swapped in passes the archive check, so
    # only the stored record (with its SHA-256) can catch it.
    other = write_zip(
        tmp_path / "other.zip",
        safe_members(files=data_files(band_bytes=20_001)),
        zipfile.ZIP_STORED,
    )
    with store_for(scratch_root, clock) as store:
        acquire(store, SyntheticTransport({download_url(): SyntheticResponse(200, archive_bytes)}))
        assert inspect(other).name == NAME
        store.archive_path(PRODUCT_ID).write_bytes(other.read_bytes())
        with refused("cache_corrupt"):
            acquire(store, SyntheticTransport())
        assert store.cached() == []


def test_o76_cache_hit_with_another_name_is_refused_without_eviction(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    with store_for(scratch_root, clock) as store:
        acquire(store, SyntheticTransport({download_url(): SyntheticResponse(200, archive_bytes)}))
        with refused("name_mismatch"):
            acquire(store, SyntheticTransport(), name=product_name(tile="T31NAB"))
        assert [item.product_id for item in store.cached()] == [PRODUCT_ID]


def test_o77_metadata_without_imagery_is_refused(tmp_path: Path) -> None:
    junk = {"junk.txt": b"not imagery\n"}
    with refused("image_structure"):
        inspect(build_safe(tmp_path / "junk.zip", files=junk, images=False))
    assert inspect(build_safe(tmp_path / "ok.zip", files=junk)).name == NAME


@pytest.mark.parametrize("band", ["B02", "B03", "B04", "B08", "SCL"])
def test_o77_each_required_image_is_needed(tmp_path: Path, band: str) -> None:
    files = {k: v for k, v in image_files().items() if f"_{band}_" not in k}
    with refused("image_structure"):
        inspect(build_safe(tmp_path / "p.zip", files=files, images=False))
    assert inspect(build_safe(tmp_path / "ok.zip", files=image_files(), images=False))


def _with_image_change(path: str, data: bytes | None = None, rename: str | None = None) -> dict:
    files = image_files()
    target = next(k for k in files if path in k)
    content = files.pop(target)
    files[rename or target] = content if data is None else data
    return files


@pytest.mark.parametrize(
    "files",
    [
        _with_image_change("_B04_", data=b"PLACEHOLDER, not a JPEG 2000 file"),
        _with_image_change("_B04_", data=JP2_SIGNATURE),
        _with_image_change(
            "_B04_",
            rename="GRANULE/L2A_T31NAB_A000001_20260214T103029/IMG_DATA/R10m/"
            "T31NAA_20260214T103029_B04_10m.jp2",
        ),
        _with_image_change(
            "_B04_",
            rename="GRANULE/L2A_T31NAA_A000001_20260214T103029/IMG_DATA/R10m/"
            "T31NAA_20260214T103030_B04_10m.jp2",
        ),
        _with_image_change(
            "_SCL_",
            rename="GRANULE/L2A_T31NAA_A000001_20260214T103029/IMG_DATA/R10m/"
            "T31NAA_20260214T103029_SCL_20m.jp2",
        ),
        {
            **image_files(),
            "GRANULE/L2A_T31NAA_A000001_20260214T103029/IMG_DATA/R20m/"
            "T31NAA_20260214T103029_B05_20m.jp2": JP2_SIGNATURE + b"x",
        },
    ],
    ids=["not-jp2", "empty", "two-granules", "mixed-times", "scl-wrong-folder", "control-extra"],
)
def test_o77_image_structure_details(tmp_path: Path, request, files: dict) -> None:
    path = build_safe(tmp_path / "p.zip", files=files, images=False)
    if request.node.callspec.id == "control-extra":
        # An extra file whose name does not match a required image is allowed.
        assert inspect(path).name == NAME
        return
    with refused("image_structure"):
        inspect(path)


def test_o77_required_image_must_be_checksum_listed(tmp_path: Path) -> None:
    files = image_files()
    members = safe_members(files=files, images=False)
    listed = {k: v for k, v in files.items() if "_B03_" not in k}
    members[f"{NAME}/manifest.safe"] = manifest_xml(listed)
    with refused("image_structure"):
        inspect(write_zip(tmp_path / "p.zip", members))


@pytest.mark.parametrize("left", [1, 4095, 4096, 4097])
def test_o78_unknown_length_never_exceeds_the_daily_allowance(
    scratch_root: Path, clock: Clock, archive_bytes: bytes, left: int
) -> None:
    size = len(archive_bytes)
    limits = ScratchLimits(10_000_000, 10, 10, size + left)
    with store_for(scratch_root, clock, limits) as store:
        acquire(
            store,
            SyntheticTransport({download_url(pid(1)): SyntheticResponse(200, archive_bytes)}),
            product_id=pid(1),
        )
        body = SyntheticResponse(200, archive_bytes, content_length=False)
        with refused("daily_byte_cap"):
            acquire(store, SyntheticTransport({download_url(pid(2)): body}), product_id=pid(2))
        assert body.bytes_served == left and max(body.read_sizes) <= left
        assert store.budget_today()["bytes"] == limits.max_bytes_per_day
        assert store.remaining_bytes_today() == 0 and partial_files(scratch_root) == []


def test_o78_unknown_length_within_the_allowance_is_accepted(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    size = len(archive_bytes)
    # The body must end before the cap, so one spare byte is the tightest fit.
    limits = ScratchLimits(10_000_000, 10, 10, 2 * size + 1)
    with store_for(scratch_root, clock, limits) as store:
        acquire(
            store,
            SyntheticTransport({download_url(pid(1)): SyntheticResponse(200, archive_bytes)}),
            product_id=pid(1),
        )
        body = SyntheticResponse(200, archive_bytes, content_length=False)
        result = acquire(store, SyntheticTransport({download_url(pid(2)): body}), product_id=pid(2))
        assert result.transferred_bytes == size
        assert store.remaining_bytes_today() == 1


def test_o78_declared_length_equal_to_the_allowance_is_accepted(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    size = len(archive_bytes)
    limits = ScratchLimits(10_000_000, 10, 10, 2 * size)
    with store_for(scratch_root, clock, limits) as store:
        acquire(
            store,
            SyntheticTransport({download_url(pid(1)): SyntheticResponse(200, archive_bytes)}),
            product_id=pid(1),
        )
        assert acquire(
            store,
            SyntheticTransport({download_url(pid(2)): SyntheticResponse(200, archive_bytes)}),
            product_id=pid(2),
        )
        assert store.remaining_bytes_today() == 0
        late = SyntheticResponse(200, archive_bytes)
        with refused("daily_byte_cap"):
            acquire(store, SyntheticTransport({download_url(pid(3)): late}), product_id=pid(3))
        assert late.read_sizes == []


def test_o78_transport_returning_more_than_asked_is_refused(
    scratch_root: Path, clock: Clock, archive_bytes: bytes
) -> None:
    class Greedy(SyntheticResponse):
        def read(self, size: int) -> bytes:
            return super().read(size * 2)

    greedy = Greedy(200, archive_bytes, content_length=False)
    with store_for(scratch_root, clock) as store:
        with refused("transfer_failed"):
            acquire(store, SyntheticTransport({download_url(): greedy}))
        assert partial_files(scratch_root) == []


# -- audit round 2: O76 wrong-typed cached record, O77 image time relation -------


@pytest.mark.parametrize(
    "change", [{"record": []}, {"record": "x"}, {"record": None}, {"last_used": 5}]
)
def test_o76_wrong_typed_cached_record_is_a_named_refusal(
    scratch_root: Path, clock: Clock, archive_bytes: bytes, change: dict
) -> None:
    with store_for(scratch_root, clock) as store:
        acquire(store, SyntheticTransport({download_url(): SyntheticResponse(200, archive_bytes)}))
        path = scratch_root / "products" / PRODUCT_ID / "record.json"
        saved = json.loads(path.read_text())
        # Control: the valid record is read back.
        assert store.cached()[0].record == saved["record"]
        path.write_text(json.dumps({**saved, **change}), encoding="utf-8")
        with refused("scratch_corrupt"):
            store.cached()
        with refused("scratch_corrupt"):
            acquire(store, SyntheticTransport())


def _images_at(image_time: str, granule_time: str = "20260214T103029") -> dict:
    files = image_files()
    return {
        k.replace("A000001_20260214T103029", f"A000001_{granule_time}").replace(
            "_20260214T103029_", f"_{image_time}_"
        ): v
        for k, v in files.items()
    }


@pytest.mark.parametrize(
    ("image_time", "granule_time", "accepted"),
    [
        ("20260214T103029", "20260214T103029", True),
        ("20260215T103029", "20260214T103029", False),
        ("20260213T103029", "20260214T103029", False),
        ("20260214T103030", "20260214T103029", False),
        ("20260215T103029", "20260215T103029", False),
        ("20260214T103029", "20260214T103028", False),
        ("20260214T103029", "20260214T103031", False),
    ],
    ids=[
        "aligned",
        "images-next-day",
        "images-previous-day",
        "images-one-second-late",
        "images-and-granule-next-day",
        "granule-before-datatake",
        "granule-after-product-stop",
    ],
)
def test_o77_image_times_follow_the_product(
    tmp_path: Path, image_time: str, granule_time: str, accepted: bool
) -> None:
    # Product: sensing start 10:30:29.024, product stop 10:30:29.024 (fixture).
    path = build_safe(tmp_path / "p.zip", files=_images_at(image_time, granule_time), images=False)
    if accepted:
        assert inspect(path).name == NAME
    else:
        with refused("image_time_mismatch"):
            inspect(path)


def test_o77_granule_time_within_the_datatake_is_accepted(tmp_path: Path) -> None:
    # Control: a granule sensed after the datatake start and before product stop.
    fields = {"PRODUCT_STOP_TIME": "2026-02-14T10:31:00Z"}
    files = _images_at("20260214T103029", granule_time="20260214T103045")
    assert inspect(build_safe(tmp_path / "ok.zip", files=files, images=False, fields=fields))
    files = _images_at("20260214T103029", granule_time="20260214T103101")
    with refused("image_time_mismatch"):
        inspect(build_safe(tmp_path / "p.zip", files=files, images=False, fields=fields))
