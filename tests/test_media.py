"""Media downloads. Same atomicity discipline as everything else that touches
the card, plus truncation detection -- a short read produces a file that looks
fine until a theme tries to render it."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from pyskraper.core.media import MediaDownloader
from pyskraper.core.models import MediaAsset

URL = "https://media.screenscraper.fr/asset.png"


def _asset(target: Path) -> MediaAsset:
    asset = MediaAsset(tag="image", key="ss", url=URL, fmt="png")
    asset.target = target
    return asset


@pytest.fixture
def downloader() -> MediaDownloader:
    return MediaDownloader(httpx.AsyncClient())


@respx.mock
async def test_downloads_and_marks_the_asset(tmp_path: Path, downloader: MediaDownloader) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"PNGDATA"))
    asset = _asset(tmp_path / "images" / "game-image.png")

    outcome = await downloader.fetch(asset)

    assert outcome.written
    assert asset.downloaded
    assert (tmp_path / "images" / "game-image.png").read_bytes() == b"PNGDATA"


@respx.mock
async def test_existing_file_is_skipped_unless_overwriting(tmp_path: Path) -> None:
    target = tmp_path / "game-image.png"
    target.write_bytes(b"already here")
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"new"))

    outcome = await MediaDownloader(httpx.AsyncClient()).fetch(_asset(target))

    assert outcome.skipped and not outcome.written
    assert route.call_count == 0, "skipping should not spend bandwidth"
    assert target.read_bytes() == b"already here"


@respx.mock
async def test_overwrite_replaces_the_file(tmp_path: Path) -> None:
    target = tmp_path / "game-image.png"
    target.write_bytes(b"old")
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"new"))

    await MediaDownloader(httpx.AsyncClient(), overwrite=True).fetch(_asset(target))
    assert target.read_bytes() == b"new"


@respx.mock
async def test_truncated_download_is_rejected(tmp_path: Path, downloader: MediaDownloader) -> None:
    """A short read against a declared content-length means a corrupt asset.
    Publishing it would put a broken image on the card."""
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"12345", headers={"content-length": "99"}))
    target = tmp_path / "game-image.png"

    outcome = await downloader.fetch(_asset(target))

    assert not outcome.written
    assert outcome.error is not None and "truncated" in outcome.error
    assert not target.exists()


@respx.mock
async def test_http_error_leaves_nothing_behind(tmp_path: Path, downloader: MediaDownloader) -> None:
    respx.get(URL).mock(return_value=httpx.Response(404, text="gone"))
    target = tmp_path / "game-image.png"

    outcome = await downloader.fetch(_asset(target))

    assert not outcome.written
    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()


@respx.mock
async def test_network_error_leaves_no_part_file(tmp_path: Path, downloader: MediaDownloader) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("no route"))
    target = tmp_path / "game-image.png"

    outcome = await downloader.fetch(_asset(target))

    assert not outcome.written
    assert not target.with_name(target.name + ".part").exists()


@respx.mock
async def test_empty_response_is_not_written(tmp_path: Path, downloader: MediaDownloader) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b""))
    target = tmp_path / "game-image.png"

    assert not (await downloader.fetch(_asset(target))).written
    assert not target.exists()


@respx.mock
async def test_previous_file_survives_a_failed_refresh(tmp_path: Path) -> None:
    """The atomic guarantee that matters on removable media: a failed download
    must never destroy the asset that was already there."""
    target = tmp_path / "game-image.png"
    target.write_bytes(b"good old image")
    respx.get(URL).mock(return_value=httpx.Response(500, text="server error"))

    await MediaDownloader(httpx.AsyncClient(), overwrite=True).fetch(_asset(target))

    assert target.read_bytes() == b"good old image"


@respx.mock
async def test_identical_urls_are_downloaded_once_and_copied(tmp_path: Path) -> None:
    """`thumbnail` and `boxart` both default to box-2D. They need different
    filenames but are identical bytes, so downloading twice doubles the transfer
    for nothing -- gigabytes across a full library."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"BOXART"))

    thumb = MediaAsset(tag="thumbnail", key="box-2D", url=URL, fmt="png")
    thumb.target = tmp_path / "images" / "game-thumb.png"
    box = MediaAsset(tag="boxart", key="box-2D", url=URL, fmt="png")
    box.target = tmp_path / "images" / "game-box.png"

    outcomes = await MediaDownloader(httpx.AsyncClient()).fetch_all([thumb, box])

    assert route.call_count == 1, "the same URL must only be fetched once"
    assert thumb.target.read_bytes() == b"BOXART"
    assert box.target.read_bytes() == b"BOXART"
    assert thumb.downloaded and box.downloaded
    assert len(outcomes) == 2
    # Only the real transfer counts toward bytes downloaded.
    assert sum(o.bytes_written for o in outcomes) == len(b"BOXART")


@respx.mock
async def test_distinct_urls_are_all_fetched(tmp_path: Path) -> None:
    other = "https://media.screenscraper.fr/other.png"
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"A"))
    respx.get(other).mock(return_value=httpx.Response(200, content=b"B"))

    a = MediaAsset(tag="image", key="ss", url=URL, fmt="png")
    a.target = tmp_path / "a.png"
    b = MediaAsset(tag="marquee", key="wheel", url=other, fmt="png")
    b.target = tmp_path / "b.png"

    await MediaDownloader(httpx.AsyncClient()).fetch_all([a, b])
    assert a.target.read_bytes() == b"A"
    assert b.target.read_bytes() == b"B"


@respx.mock
async def test_failed_shared_download_does_not_create_copies(tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(500, text="boom"))

    thumb = MediaAsset(tag="thumbnail", key="box-2D", url=URL, fmt="png")
    thumb.target = tmp_path / "game-thumb.png"
    box = MediaAsset(tag="boxart", key="box-2D", url=URL, fmt="png")
    box.target = tmp_path / "game-box.png"

    outcomes = await MediaDownloader(httpx.AsyncClient()).fetch_all([thumb, box])

    assert not any(o.written for o in outcomes)
    assert not thumb.target.exists() and not box.target.exists()
