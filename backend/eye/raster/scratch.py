"""Bounded scratch and product cache for the raster worker (Package 6).

The cache holds only re-fetchable provider archives, never EYE capture
history, so evicting one loses nothing that cannot be fetched again; the
layer then reports that product as missing, not as unchanged.

Layout under the scratch root (named by an environment variable, never in
the configuration file or repository):

    products/<product id>/product.zip   checked archive
    products/<product id>/record.json   its ProductRecord and last use time
    partial/<product id>.zip            a download in progress
    budget.json                         products and bytes started this UTC day
    .lock                               held by the one process using the root

Limits (``[imagery]``): total bytes on scratch, number of cached products,
products and bytes started per UTC day. A download reserves its full
``max_product_bytes`` before it starts, evicting least recently used products
only when that makes room; if evicting everything would not, nothing is
evicted and the request is refused. A damaged ``record.json`` or
``budget.json`` stops the store (fail closed) rather than being guessed at.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eye.raster.sentinel2 import ImageryRefused, ProductRecord, check_product_id

RECORD = "record.json"
ARCHIVE = "product.zip"


@dataclass(frozen=True)
class ScratchLimits:
    max_scratch_bytes: int
    max_cached_products: int
    max_products_per_day: int
    max_bytes_per_day: int


@dataclass(frozen=True)
class CachedProduct:
    product_id: str
    bytes: int
    last_used: str
    record: dict


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ScratchStore:
    """One process's bounded imagery scratch. Use as a context manager."""

    def __init__(self, root: Path, limits: ScratchLimits, clock: Callable[[], datetime]):
        self.root = root
        self.limits = limits
        self.clock = clock
        self._lock_handle = None

    def __enter__(self) -> ScratchStore:
        if not self.root.is_dir():
            raise ImageryRefused("scratch_missing", "imagery scratch directory does not exist")
        handle = (self.root / ".lock").open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ImageryRefused("scratch_locked", "another process is using this scratch") from exc
        self._lock_handle = handle
        (self.root / "products").mkdir(exist_ok=True)
        partial = self.root / "partial"
        partial.mkdir(exist_ok=True)
        # A partial file left by a stopped process was never admitted.
        for leftover in partial.iterdir():
            leftover.unlink()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def _require_open(self) -> None:
        if self._lock_handle is None:
            raise ImageryRefused("scratch_locked", "scratch store is not open")

    # -- accounting -------------------------------------------------------

    def used_bytes(self) -> int:
        self._require_open()
        total = 0
        for folder in ("products", "partial"):
            for path in (self.root / folder).rglob("*"):
                if path.is_symlink():
                    raise ImageryRefused(
                        "scratch_corrupt", f"symbolic link in scratch: {path.name}"
                    )
                if path.is_file():
                    total += path.stat().st_size
        return total

    def cached(self) -> list[CachedProduct]:
        """Cached products, least recently used first."""
        self._require_open()
        items = []
        for folder in sorted((self.root / "products").iterdir()):
            try:
                data = json.loads((folder / RECORD).read_text(encoding="utf-8"))
                last_used = data["last_used"]
                record = data["record"]
                size = (folder / ARCHIVE).stat().st_size
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ImageryRefused(
                    "scratch_corrupt", f"cached product {folder.name} has no readable record"
                ) from exc
            if not isinstance(last_used, str) or record.get("product_id") != folder.name:
                raise ImageryRefused("scratch_corrupt", f"cached product {folder.name} is damaged")
            items.append(CachedProduct(folder.name, size, last_used, record))
        return sorted(items, key=lambda item: (item.last_used, item.product_id))

    def _budget(self) -> dict:
        today = self.clock().astimezone(UTC).date().isoformat()
        path = self.root / "budget.json"
        if not path.exists():
            return {"day": today, "products": 0, "bytes": 0}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            valid = isinstance(data.get("day"), str) and all(
                isinstance(data.get(k), int) and not isinstance(data.get(k), bool) and data[k] >= 0
                for k in ("products", "bytes")
            )
        except (OSError, ValueError, AttributeError) as exc:
            raise ImageryRefused("scratch_corrupt", "budget.json is unreadable") from exc
        if not valid:
            raise ImageryRefused("scratch_corrupt", "budget.json is damaged")
        if data["day"] > today:
            raise ImageryRefused("scratch_corrupt", "budget.json is dated after today")
        if data["day"] < today:
            return {"day": today, "products": 0, "bytes": 0}
        return data

    def _write_json(self, path: Path, data: dict) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def budget_today(self) -> dict:
        self._require_open()
        return dict(self._budget())

    def remaining_bytes_today(self) -> int:
        self._require_open()
        return max(self.limits.max_bytes_per_day - self._budget()["bytes"], 0)

    def record_transfer(self, transferred: int) -> None:
        """Count bytes received from the provider, admitted or not."""
        self._require_open()
        budget = self._budget()
        budget["bytes"] += transferred
        self._write_json(self.root / "budget.json", budget)

    # -- reservation, admission and eviction -----------------------------

    def get(self, product_id: str) -> CachedProduct | None:
        """A cached product (and mark it used), or None."""
        self._require_open()
        check_product_id(product_id)
        for item in self.cached():
            if item.product_id == product_id:
                self._touch(item)
                return item
        return None

    def _touch(self, item: CachedProduct) -> None:
        folder = self.root / "products" / item.product_id
        self._write_json(
            folder / RECORD, {"last_used": _stamp(self.clock()), "record": item.record}
        )

    def reserve(self, product_id: str, nbytes: int) -> tuple[Path, list[CachedProduct]]:
        """Admit one download of at most ``nbytes``. Returns its partial path and evictions.

        Checks the daily caps first, then makes room by evicting least
        recently used products. Nothing is evicted if the room cannot be made.
        """
        self._require_open()
        check_product_id(product_id)
        if (self.root / "products" / product_id).exists():
            raise ImageryRefused("already_cached", f"{product_id} is already cached")
        budget = self._budget()
        if budget["products"] >= self.limits.max_products_per_day:
            raise ImageryRefused(
                "daily_product_cap",
                f"{budget['products']} products started today; limit "
                f"{self.limits.max_products_per_day}",
            )
        if budget["bytes"] >= self.limits.max_bytes_per_day:
            raise ImageryRefused(
                "daily_byte_cap",
                f"{budget['bytes']} bytes today; limit {self.limits.max_bytes_per_day}",
            )
        if nbytes > self.limits.max_scratch_bytes:
            raise ImageryRefused(
                "scratch_full", f"{nbytes} bytes cannot fit {self.limits.max_scratch_bytes}"
            )
        cached = self.cached()
        used = self.used_bytes()
        plan: list[CachedProduct] = []
        for item in cached:
            fits = self.limits.max_scratch_bytes - used >= nbytes
            count_ok = len(cached) - len(plan) < self.limits.max_cached_products
            if fits and count_ok:
                break
            plan.append(item)
            used -= item.bytes
        if self.limits.max_scratch_bytes - used < nbytes:
            raise ImageryRefused("scratch_full", "scratch cannot hold another product")
        for item in plan:
            shutil.rmtree(self.root / "products" / item.product_id)
        budget["products"] += 1
        self._write_json(self.root / "budget.json", budget)
        return self.root / "partial" / f"{product_id}.zip", plan

    def admit(self, partial: Path, record: ProductRecord) -> CachedProduct:
        """Move a checked archive from partial into the cache."""
        self._require_open()
        expected = self.root / "partial" / f"{record.product_id}.zip"
        if partial != expected or not partial.is_file():
            raise ImageryRefused("scratch_corrupt", "partial download is not where it was reserved")
        folder = self.root / "products" / record.product_id
        folder.mkdir()
        os.replace(partial, folder / ARCHIVE)
        item = CachedProduct(
            record.product_id, (folder / ARCHIVE).stat().st_size, "", record.to_json()
        )
        self._touch(item)
        return self.get(record.product_id)  # type: ignore[return-value]

    def discard(self, partial: Path) -> None:
        """Delete a refused or failed download."""
        self._require_open()
        if partial.parent == self.root / "partial":
            partial.unlink(missing_ok=True)
