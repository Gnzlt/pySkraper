"""Downloading media onto the card.

Two things make this different from an ordinary download helper:

* **Atomicity is mandatory.** The destination is a removable card with no
  Trash.  Everything streams to ``<target>.part`` and is ``os.replace``-d into
  position, so a yanked card costs the current file and nothing else.
* **Media does not spend the request quota.** Asset downloads count against
  ``maxdownloadspeed``, a separate allowance, so they must not be funnelled
  through the request governor -- doing so would throttle a 40-file download
  batch to the per-minute *lookup* rate for no reason.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from .atomic import atomic_copy, part_path
from .models import MediaAsset

__all__ = ["DownloadOutcome", "MediaDownloader"]

log = logging.getLogger(__name__)

CHUNK = 64 * 1024


@dataclass
class DownloadOutcome:
    asset: MediaAsset
    written: bool
    skipped: bool = False
    error: str | None = None
    bytes_written: int = 0


class MediaDownloader:
    """Streams assets to disk with bounded concurrency."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        concurrency: int = 4,
        overwrite: bool = False,
        timeout: float = 120.0,
    ) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._overwrite = overwrite
        self._timeout = timeout

    async def fetch(self, asset: MediaAsset) -> DownloadOutcome:
        target = asset.target
        if target is None:
            return DownloadOutcome(asset, written=False, error="no target path")

        if target.exists() and not self._overwrite:
            asset.downloaded = True
            return DownloadOutcome(asset, written=False, skipped=True)

        async with self._semaphore:
            return await self._stream_to(asset, target)

    async def _stream_to(self, asset: MediaAsset, target: Path) -> DownloadOutcome:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = part_path(target)
        written = 0

        try:
            async with self._client.stream("GET", asset.url, timeout=self._timeout) as response:
                if response.status_code >= 400:
                    await response.aread()
                    return DownloadOutcome(asset, written=False, error=f"HTTP {response.status_code}")

                declared = response.headers.get("content-length")
                expected = int(declared) if declared and declared.isdigit() else None

                with open(tmp, "wb") as handle:
                    async for block in response.aiter_bytes(CHUNK):
                        handle.write(block)
                        written += len(block)
                    handle.flush()
                    os.fsync(handle.fileno())

            if expected is not None and written != expected:
                # A short read means a truncated asset.  Publishing it would put
                # a corrupt image on the card that looks fine until the theme
                # tries to render it.
                tmp.unlink(missing_ok=True)
                return DownloadOutcome(asset, written=False, error=f"truncated: {written}/{expected} bytes")

            if written == 0:
                tmp.unlink(missing_ok=True)
                return DownloadOutcome(asset, written=False, error="empty response")

            os.replace(tmp, target)
        except (httpx.HTTPError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            return DownloadOutcome(asset, written=False, error=f"{type(exc).__name__}: {exc}")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        asset.downloaded = True
        return DownloadOutcome(asset, written=True, bytes_written=written)

    async def fetch_all(self, assets: list[MediaAsset]) -> list[DownloadOutcome]:
        """Fetch every asset, downloading each distinct URL only once.

        Several gamelist tags routinely resolve to the same ScreenScraper asset
        -- `thumbnail` and `boxart` both default to `box-2D`, for instance. They
        need different filenames on the card, but they are identical bytes, so
        downloading each separately doubles both the transfer and the wait for
        no benefit. On a full library that is gigabytes.
        """
        if not assets:
            return []

        by_url: dict[str, list[MediaAsset]] = {}
        for asset in assets:
            by_url.setdefault(asset.url, []).append(asset)

        leaders = [group[0] for group in by_url.values()]
        outcomes = list(await asyncio.gather(*(self.fetch(a) for a in leaders)))

        extra: list[DownloadOutcome] = []
        for outcome in outcomes:
            duplicates = by_url[outcome.asset.url][1:]
            for duplicate in duplicates:
                extra.append(await self._copy_from(outcome, duplicate))

        return outcomes + extra

    async def _copy_from(self, source: DownloadOutcome, target_asset: MediaAsset) -> DownloadOutcome:
        """Materialise a second filename for an asset already fetched."""
        target = target_asset.target
        origin = source.asset.target
        if target is None or origin is None or not origin.exists():
            return DownloadOutcome(target_asset, written=False, error=source.error or "source unavailable")

        if target.exists() and not self._overwrite:
            target_asset.downloaded = True
            return DownloadOutcome(target_asset, written=False, skipped=True)

        try:
            # atomic_copy rather than copyfile + replace: it fsyncs, it checks
            # the length before publishing, and it removes the part file on
            # cancellation as well as on OSError.
            await asyncio.to_thread(atomic_copy, origin, target)
        except OSError as exc:
            return DownloadOutcome(target_asset, written=False, error=f"copy failed: {exc}")

        target_asset.downloaded = True
        # Not counted as bytes_downloaded: nothing crossed the network.
        return DownloadOutcome(target_asset, written=True, bytes_written=0)
