"""What to hash when the file on the card is a container.

This is the difference between a hit and a wasted KO request, and it is not a
matter of taste:

* **Arcade** entries in ScreenScraper are keyed on the hash of the ``.zip``
  itself, because the zip *is* the ROM set as MAME loads it.
* **Console** entries are keyed on the hash of the raw ROM, because that is
  what No-Intro catalogues.  A zipped SNES ROM must therefore be hashed by its
  *contents*, not by the archive.

Hashing the wrong one is a guaranteed miss, and every miss spends the scarce
KO budget.  ``hash_archives: both`` tries each in turn, which is cheap once the
hash cache is warm.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Iterator
from pathlib import Path

from ..systems import SystemInfo
from .hasher import Hashes, hash_file, hash_stream

__all__ = ["ARCHIVE_SUFFIXES", "candidate_hashes", "hash_archive_contents", "is_archive"]

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = frozenset({".zip", ".7z"})

# Files inside an archive that are never the game.
_NON_ROM_SUFFIXES = frozenset({".txt", ".nfo", ".diz", ".jpg", ".png", ".xml", ".md", ".sbi", ".cue"})


def is_archive(path: Path) -> bool:
    return path.suffix.lower() in ARCHIVE_SUFFIXES


def hash_archive_contents(path: Path, *, max_size: int = 0) -> Hashes | None:
    """Hash the ROM inside a ``.zip`` without extracting it to disk.

    ``.7z`` is deliberately not supported: it would add a dependency for a
    format that is rare on handheld cards, and the honest fallback (hash the
    archive itself) is already in the chain.  A 7z archive returns ``None`` here
    and is identified some other way rather than being silently mis-hashed.
    """
    if path.suffix.lower() != ".zip":
        return None

    try:
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            candidates = [m for m in members if Path(m.filename).suffix.lower() not in _NON_ROM_SUFFIXES]
            chosen = candidates or members
            if not chosen:
                return None
            # The biggest member is the ROM; the rest is documentation.
            target = max(chosen, key=lambda m: m.file_size)
            with archive.open(target) as handle:
                return hash_stream(handle, max_size=max_size)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        log.debug("Could not read inside %s: %s", path.name, exc)
        return None


def candidate_hashes(
    path: Path,
    info: SystemInfo,
    *,
    policy: str = "auto",
    max_size: int = 0,
    file_hashes: Hashes | None = None,
) -> Iterator[tuple[str, Hashes]]:
    """Yield ``(source, hashes)`` pairs to try, in priority order.

    ``source`` is ``"archive"`` or ``"contents"``, and is reported in the run
    summary so a user can see *how* their zipped library matched.

    ``file_hashes`` is the outer file's hashes when the caller already has
    them. Identification computes them for cache keying immediately before
    calling here, so without this every plain ROM got read from the card twice.
    """
    if not is_archive(path):
        yield "file", file_hashes if file_hashes is not None else hash_file(path, max_size=max_size)
        return

    effective = info.archive_policy if policy == "auto" else policy

    order: tuple[str, ...]
    if effective == "archive":
        order = ("archive",)
    elif effective == "contents":
        order = ("contents",)
    else:  # "both"
        order = ("contents", "archive") if info.archive_policy == "contents" else ("archive", "contents")

    for source in order:
        if source == "archive":
            # "archive" means the outer file, which is exactly what
            # ``file_hashes`` holds when the caller supplied it.
            yield "archive", file_hashes if file_hashes is not None else hash_file(path, max_size=max_size)
        else:
            inner = hash_archive_contents(path, max_size=max_size)
            if inner is not None:
                yield "contents", inner
