"""Invented Sentinel-2 L2A SAFE-shaped archives and an offline transport (Package 6).

Nothing here is real imagery or a real product. Every "band" file is a short
text placeholder, the product id is an invented UUID and no metadata value was
copied from a real product. Archives are built in each test's temporary
directory; none is committed. The transport answers from a fixed table and
records every request; it has no network code at all.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path

PRODUCT_ID = "0e7e5000-0000-4000-8000-000000000001"
NAME = "S2B_MSIL2A_20260214T103029_N0511_R108_T31NAA_20260214T125512.SAFE"
TOKEN = "synthetic-bearer-token-6c2f"
JP2_SIGNATURE = b"\x00\x00\x00\x0cjP  \r\n\x87\n"
PLACEHOLDER = b"EYE synthetic placeholder, not imagery\n"

MTD_DEFAULTS = {
    "PRODUCT_START_TIME": "2026-02-14T10:30:29.024Z",
    "PRODUCT_STOP_TIME": "2026-02-14T10:30:29.024Z",
    "PRODUCT_URI": NAME,
    "PROCESSING_LEVEL": "Level-2A",
    "PRODUCT_TYPE": "S2MSI2A",
    "PROCESSING_BASELINE": "05.11",
    "GENERATION_TIME": "2026-02-14T12:55:12.000000Z",
    "SPACECRAFT_NAME": "Sentinel-2B",
    "DATATAKE_SENSING_START": "2026-02-14T10:30:29.024Z",
}


def product_name(
    *,
    mission: str = "S2B",
    sensing: str = "20260214T103029",
    baseline: str = "N0511",
    tile: str = "T31NAA",
    discriminator: str = "20260214T125512",
) -> str:
    return f"{mission}_MSIL2A_{sensing}_{baseline}_R108_{tile}_{discriminator}.SAFE"


def mtd_xml(fields: Mapping[str, str | None] | None = None, extra: str = "") -> bytes:
    """MTD_MSIL2A.xml-shaped metadata. A field set to None is left out."""
    values = {**MTD_DEFAULTS, **(fields or {})}
    product_info = "".join(
        f"<{k}>{values[k]}</{k}>"
        for k in (
            "PRODUCT_START_TIME",
            "PRODUCT_STOP_TIME",
            "PRODUCT_URI",
            "PROCESSING_LEVEL",
            "PRODUCT_TYPE",
            "PROCESSING_BASELINE",
            "GENERATION_TIME",
        )
        if values.get(k) is not None
    )
    datatake = "".join(
        f"<{k}>{values[k]}</{k}>"
        for k in ("SPACECRAFT_NAME", "DATATAKE_SENSING_START")
        if values.get(k) is not None
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<n1:Level-2A_User_Product xmlns:n1="urn:eye:synthetic:l2a">'
        f"<n1:General_Info><Product_Info>{product_info}"
        f"<Datatake>{datatake}</Datatake></Product_Info>{extra}</n1:General_Info>"
        "</n1:Level-2A_User_Product>\n"
    ).encode()


def manifest_xml(
    files: Mapping[str, bytes], algorithm: str = "MD5", digests: Mapping[str, str] | None = None
) -> bytes:
    """manifest.safe-shaped listing with one checksum per data file."""
    hasher = {"MD5": "md5", "SHA3-256": "sha3_256"}.get(algorithm, "md5")
    objects = []
    for index, (path, data) in enumerate(sorted(files.items())):
        digest = (digests or {}).get(path) or hashlib.new(hasher, data).hexdigest()
        objects.append(
            f'<dataObject ID="obj{index}"><byteStream mimeType="application/octet-stream">'
            f'<fileLocation locatorType="URL" href="./{path}"/>'
            f'<checksum checksumName="{algorithm}">{digest}</checksum>'
            "</byteStream></dataObject>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xfdu:XFDU xmlns:xfdu="urn:eye:synthetic:xfdu">'
        f"<dataObjectSection>{''.join(objects)}</dataObjectSection></xfdu:XFDU>\n"
    ).encode()


def image_files(tile: str = "31NAA", band_bytes: int = len(PLACEHOLDER) * 4) -> dict[str, bytes]:
    """The minimum L2A image set: B02, B03, B04, B08 at 10 m and SCL at 20 m.

    Each starts with the 12-byte JPEG 2000 signature box and is otherwise an
    invented text placeholder, not imagery.
    """
    granule = f"GRANULE/L2A_T{tile}_A000001_20260214T103029"
    files = {}
    for resolution, band, suffix in (
        ("R10m", "B02", "10m"),
        ("R10m", "B03", "10m"),
        ("R10m", "B04", "10m"),
        ("R10m", "B08", "10m"),
        ("R20m", "SCL", "20m"),
    ):
        filler = band.encode() + b" " + PLACEHOLDER
        body = (filler * (band_bytes // len(filler) + 1))[:band_bytes]
        path = f"{granule}/IMG_DATA/{resolution}/T{tile}_20260214T103029_{band}_{suffix}.jp2"
        files[path] = JP2_SIGNATURE + body
    return files


def data_files(band_bytes: int = len(PLACEHOLDER) * 4, tile: str = "31NAA") -> dict[str, bytes]:
    """The required images plus one quality file: a complete synthetic product."""
    return {
        **image_files(tile, band_bytes),
        f"GRANULE/L2A_T{tile}_A000001_20260214T103029/QI_DATA/MSK_CLDPRB_20m.jp2": PLACEHOLDER,
    }


def safe_members(
    *,
    name: str = NAME,
    fields: Mapping[str, str | None] | None = None,
    files: Mapping[str, bytes] | None = None,
    algorithm: str = "MD5",
    images: bool = True,
) -> dict[str, bytes]:
    """Members of a valid synthetic archive, keyed by full member name.

    The required images for the name's tile are always included (and listed
    in the manifest) unless ``images`` is false; ``files`` adds to them.
    """
    tile = name.split("_")[5][1:] if name.count("_") >= 6 else "31NAA"
    extra = dict(data_files(tile=tile) if files is None else files)
    files = {**(image_files(tile) if images else {}), **extra}
    fields = {"PRODUCT_URI": name, **(fields or {})}
    members = {f"{name}/{path}": data for path, data in files.items()}
    members[f"{name}/MTD_MSIL2A.xml"] = mtd_xml(fields)
    members[f"{name}/manifest.safe"] = manifest_xml(files, algorithm)
    return members


def write_zip(
    path: Path,
    members: Mapping[str, bytes] | Iterable[tuple[zipfile.ZipInfo | str, bytes]],
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    items = members.items() if isinstance(members, Mapping) else members
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for member, data in items:
            archive.writestr(member, data)
    return path


def build_safe(path: Path, **kwargs) -> Path:
    return write_zip(path, safe_members(**kwargs))


class SyntheticResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        *,
        content_length: bool = True,
    ):
        self.status = status
        self.headers = dict(headers or {})
        if status == 200 and content_length:
            self.headers.setdefault("Content-Length", str(len(body)))
        self._body = body
        self._offset = 0
        self.read_sizes: list[int] = []
        self.closed = False

    @property
    def bytes_served(self) -> int:
        return self._offset

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class SyntheticTransport:
    """Answers GET requests from a table. An unrouted URL fails the test."""

    def __init__(self, routes: Mapping[str, SyntheticResponse] | None = None):
        self.routes = dict(routes or {})
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: Mapping[str, str]) -> SyntheticResponse:
        self.requests.append((url, dict(headers)))
        if url not in self.routes:
            raise AssertionError(f"unrouted synthetic request: {url}")
        return self.routes[url]


def download_url(product_id: str = PRODUCT_ID) -> str:
    return f"https://download.dataspace.copernicus.eu/odata/v1/Products({product_id})/$value"
