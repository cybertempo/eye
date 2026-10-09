"""Copernicus Sentinel-2 L2A download adapter (Package 6). DISABLED.

Source: ``copernicus-sentinel-2-l2a``, a **disabled candidate** in
``docs/source-policy-register.md``. This repository contains no network
transport for it: ``transport_from_config`` always refuses, and the only
transports that exist are the synthetic ones in the tests. Enabling a real
download needs the owner's imagery decision, an approved register row and a
private transport, none of which exist yet.

Ingest owns the bearer token (the brief keeps login secrets out of the raster
worker). The token comes from a caller-supplied function and is sent only in
an ``Authorization`` header, only to a destination that passes
``check_destination``; it never appears in an error message.

Destination and redirect rules (enforced before every request, so an unlisted
destination receives nothing at all, not even a connection):

- scheme ``https``; the network location is exactly
  ``download.dataspace.copernicus.eu`` (no port, user name, password, other
  case or trailing dot); no fragment; no whitespace, control character or
  backslash anywhere in the URL;
- the first request is ``/odata/v1/Products(<id>)/$value`` for a lower-case
  UUID; a redirect may go to another path on the same host only;
- at most ``max_redirects`` redirects (301, 302, 303, 307, 308), each with a
  ``Location``, and no URL visited twice.

Other documented CDSE hosts (catalogue, STAC, identity) are refused here:
this adapter only downloads. Where CDSE really redirects a download is
UNVERIFIED (register row), so any redirect off the download host fails closed
until a measured redirect is recorded and reviewed.

Size rules: a ``Content-Length`` above the cap is refused before the body is
read; the body is streamed in ``chunk_bytes`` pieces and stopped as soon as it
passes the smaller of ``max_product_bytes`` and the bytes left in today's
budget; a body shorter or longer than its ``Content-Length`` is refused.
Every byte received counts against the daily budget, admitted or not.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from eye.config import ImageryConfig
from eye.raster.scratch import CachedProduct, ScratchLimits, ScratchStore
from eye.raster.sentinel2 import (
    SOURCE_ID,
    ArchiveLimits,
    ImageryRefused,
    check_product_id,
    inspect_archive,
    parse_product_name,
)

DOWNLOAD_HOST = "download.dataspace.copernicus.eu"
ALLOWED_HOSTS = frozenset({DOWNLOAD_HOST})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_UNSAFE_URL = re.compile(r"[\x00-\x20\x7f\\]")


class Response(Protocol):
    status: int
    headers: Mapping[str, str]  # lower-case names

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class Transport(Protocol):
    """Sends one GET and returns the response without following redirects."""

    def get(self, url: str, headers: Mapping[str, str]) -> Response: ...


@dataclass(frozen=True)
class DownloadLimits:
    max_product_bytes: int
    max_redirects: int
    chunk_bytes: int


@dataclass(frozen=True)
class AcquireResult:
    product: CachedProduct
    evicted: tuple[CachedProduct, ...]
    from_cache: bool
    transferred_bytes: int


def limits_from_config(
    imagery: ImageryConfig,
) -> tuple[DownloadLimits, ArchiveLimits, ScratchLimits]:
    """The download, archive and scratch limits named in ``[imagery]``."""
    return (
        DownloadLimits(imagery.max_product_bytes, imagery.max_redirects, imagery.chunk_bytes),
        ArchiveLimits(
            max_product_bytes=imagery.max_product_bytes,
            max_archive_members=imagery.max_archive_members,
            max_uncompressed_bytes=imagery.max_uncompressed_bytes,
            max_compression_ratio=imagery.max_compression_ratio,
            max_metadata_bytes=imagery.max_metadata_bytes,
            chunk_bytes=imagery.chunk_bytes,
        ),
        ScratchLimits(
            max_scratch_bytes=imagery.max_scratch_bytes,
            max_cached_products=imagery.max_cached_products,
            max_products_per_day=imagery.max_products_per_day,
            max_bytes_per_day=imagery.max_bytes_per_day,
        ),
    )


def transport_from_config(config: object) -> Transport:
    """The real transport. Always refused: the source is a disabled candidate."""
    raise ImageryRefused(
        "source_disabled",
        f"{SOURCE_ID} is a disabled candidate in docs/source-policy-register.md; "
        "this repository has no real download transport",
    )


def download_url(product_id: str) -> str:
    check_product_id(product_id)
    return f"https://{DOWNLOAD_HOST}/odata/v1/Products({product_id})/$value"


def check_destination(url: str) -> str:
    """Return the URL if a bearer token may be sent to it; refuse otherwise."""
    if not isinstance(url, str) or _UNSAFE_URL.search(url):
        raise ImageryRefused("destination_refused", "URL has whitespace or control characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ImageryRefused("destination_refused", "URL cannot be parsed") from exc
    if parts.scheme != "https":
        raise ImageryRefused("destination_refused", "only https destinations are allowed")
    if parts.netloc not in ALLOWED_HOSTS:
        # Report the host only: a user name or password in the URL is not echoed.
        host = parts.hostname or "?"
        raise ImageryRefused("destination_refused", f"host {host!r} is not an allowed destination")
    if parts.fragment:
        raise ImageryRefused("destination_refused", "URL fragments are not allowed")
    return url


def _header(response: Response, name: str) -> str | None:
    for key, value in response.headers.items():
        if key.lower() == name:
            return value
    return None


def _open(
    product_id: str,
    transport: Transport,
    token_source: Callable[[], str],
    limits: DownloadLimits,
) -> Response:
    url = check_destination(download_url(product_id))
    visited = {url}
    for _request in range(limits.max_redirects + 1):
        # The token is fetched per request so a refreshed one is used; it is
        # attached only after the destination check above has passed.
        token = token_source()
        if not isinstance(token, str) or not token or _UNSAFE_URL.search(token):
            raise ImageryRefused("token_invalid", "the token source returned no usable token")
        try:
            response = transport.get(url, {"Authorization": f"Bearer {token}"})
        except OSError as exc:
            raise ImageryRefused(
                "provider_unavailable", "connection failed; coverage unknown"
            ) from exc
        status = response.status
        if status == 200:
            return response
        response.close()
        if status in REDIRECT_STATUSES:
            location = _header(response, "location")
            if not location:
                raise ImageryRefused("redirect_without_location", f"HTTP {status} has no Location")
            target = check_destination(urljoin(url, location))
            if target in visited:
                raise ImageryRefused("redirect_loop", "redirect returns to a visited URL")
            visited.add(target)
            url = target
            continue
        if status in (401, 403):
            raise ImageryRefused(
                "provider_auth_refused", f"provider refused access (HTTP {status})"
            )
        if status == 404:
            raise ImageryRefused("provider_not_found", "provider has no such product (HTTP 404)")
        if status == 429 or 500 <= status <= 599:
            raise ImageryRefused(
                "provider_unavailable", f"provider unavailable (HTTP {status}); coverage unknown"
            )
        raise ImageryRefused("provider_status", f"unexpected HTTP status {status}")
    raise ImageryRefused("redirect_limit", f"more than {limits.max_redirects} redirects")


def _stream(response: Response, sink: Path, cap: int, chunk_bytes: int) -> tuple[int, str | None]:
    """Write the body to ``sink``; return (bytes received, refusal code or None)."""
    declared_text = _header(response, "content-length")
    declared: int | None = None
    if declared_text is not None:
        if not re.fullmatch(r"\d{1,15}", declared_text.strip()):
            return 0, "length_invalid"
        declared = int(declared_text)
        if declared > cap:
            return 0, "product_too_large"
    # A body may not run past its own declared length, nor past the cap.
    limit = cap if declared is None else declared
    received = 0
    with sink.open("xb") as out:
        while True:
            try:
                chunk = response.read(chunk_bytes)
            except OSError:
                return received, "transfer_failed"
            if not chunk:
                break
            received += len(chunk)
            if received > limit:
                return received, "product_too_large" if declared is None else "length_mismatch"
            out.write(chunk)
    if declared is not None and received != declared:
        return received, "length_mismatch"
    return received, None


def acquire_product(
    product_id: str,
    product_name: str,
    *,
    store: ScratchStore,
    transport: Transport,
    token_source: Callable[[], str],
    download: DownloadLimits,
    archive: ArchiveLimits,
    now: Callable[[], datetime],
) -> AcquireResult:
    """Fetch, check and cache one named Level-2A product, or refuse.

    A product already in the cache is returned without any request. Any
    refusal leaves no partial file behind.
    """
    check_product_id(product_id)
    parse_product_name(product_name)
    cached = store.get(product_id)
    if cached is not None:
        if cached.record.get("name") != product_name:
            raise ImageryRefused("name_mismatch", "cached product has a different name")
        return AcquireResult(cached, (), True, 0)

    partial, evicted = store.reserve(product_id, download.max_product_bytes)
    transferred = 0
    try:
        response = _open(product_id, transport, token_source, download)
        try:
            cap = min(download.max_product_bytes, store.remaining_bytes_today())
            transferred, refusal = _stream(response, partial, cap, download.chunk_bytes)
        finally:
            response.close()
        if refusal == "product_too_large" and cap < download.max_product_bytes:
            refusal = "daily_byte_cap"
        if refusal is not None:
            raise ImageryRefused(refusal, f"download refused after {transferred} bytes")
        record = inspect_archive(
            partial, product_id=product_id, expected_name=product_name, limits=archive, now=now()
        )
        product = store.admit(partial, record)
    except BaseException:
        store.discard(partial)
        raise
    finally:
        if transferred:
            store.record_transfer(transferred)
    return AcquireResult(product, tuple(evicted), False, transferred)
