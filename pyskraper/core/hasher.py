"""Content hashing.

All three hashes come out of a *single* streaming pass.  ScreenScraper accepts
CRC32, MD5 and SHA1 together on one lookup, and sending all three is strictly
better than sending one: different catalogues in their database are keyed on
different algorithms, so three hashes is three chances to match on the first
request rather than three requests.

There is deliberately no size cap by default.  ``batocera-emulationstation``
skips hashing above 128 MB, which is the right call *on the handheld* -- hashing
a 700 MB disc image on a 1.5 GHz A53 reading from microSD takes minutes.  This
tool runs on a Mac against a card reader, where the same hash is I/O-bound and
takes seconds, and the result is cached afterwards.
"""

from __future__ import annotations

import asyncio
import hashlib
import zlib
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Hashes", "hash_file", "hash_file_async", "hash_stream"]

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Hashes:
    crc32: str
    md5: str
    sha1: str
    size: int
    truncated: bool = False
    """True when a size cap stopped the read early, so the hashes cover a prefix
    only and must not be sent to the API as if they identified the whole file."""

    def as_lookup(self) -> dict[str, str]:
        """The hash parameters for a ``jeuInfos.php`` call."""
        if self.truncated:
            return {}
        return {"crc": self.crc32, "md5": self.md5, "sha1": self.sha1}


def hash_stream(stream: object, *, max_size: int = 0, chunk_size: int = CHUNK_SIZE) -> Hashes:
    """Hash a readable binary stream in one pass."""
    read = getattr(stream, "read")  # noqa: B009 - duck-typed for archives and files
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    crc = 0
    size = 0
    truncated = False

    while True:
        if max_size > 0 and size >= max_size:
            truncated = True
            break
        want = chunk_size
        if max_size > 0:
            want = min(want, max_size - size)
        block = read(want)
        if not block:
            break
        md5.update(block)
        sha1.update(block)
        crc = zlib.crc32(block, crc)
        size += len(block)

    return Hashes(
        crc32=f"{crc & 0xFFFFFFFF:08X}",
        md5=md5.hexdigest(),
        sha1=sha1.hexdigest(),
        size=size,
        truncated=truncated,
    )


def hash_file(path: Path, *, max_size: int = 0, chunk_size: int = CHUNK_SIZE) -> Hashes:
    """Hash a file on disk.  ``max_size`` of 0 means no limit."""
    with open(path, "rb") as handle:
        hashes = hash_stream(handle, max_size=max_size, chunk_size=chunk_size)

    if hashes.truncated:
        # Report the real file size even though we only read a prefix: romtaille
        # must describe the file, not our read.
        return Hashes(
            crc32=hashes.crc32,
            md5=hashes.md5,
            sha1=hashes.sha1,
            size=path.stat().st_size,
            truncated=True,
        )
    return hashes


async def hash_file_async(path: Path, *, max_size: int = 0, chunk_size: int = CHUNK_SIZE) -> Hashes:
    """Hash off the event loop.  Hashing is CPU- and IO-bound; blocking here
    would stall every in-flight HTTP request."""
    return await asyncio.to_thread(hash_file, path, max_size=max_size, chunk_size=chunk_size)
