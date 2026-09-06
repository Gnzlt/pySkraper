"""Library verification: what changed, and what is left over.

Where ``dedupe`` asks "is this the same thing twice", ``verify`` asks "is the
library still internally consistent". Four questions, all answered from the
hash index and the front-end's own metadata file, none of them costing a
request:

* **drift** -- a ROM whose content no longer matches what was hashed. A file
  that changed under a cached hash is either a re-dump or a corruption, and
  either way the metadata on the card now describes a different file.
* **missing** -- a metadata entry pointing at a ROM that is gone.
* **orphan media** -- artwork for a ROM that is gone.
* **unlisted** -- a ROM the metadata file has never heard of, i.e. not scraped.

There is a fifth thing worth finding, though it is not drift in the library
itself: **legacy media** -- a whole folder of art beside the ROMs that this
writer did not put there and does not track, left over from an earlier,
different scraper. It cannot be matched to a ROM the way ``images/`` can,
because nothing here knows that folder's naming scheme, so it is judged on
two conservative tests instead: *every* file in it looks like art or a
manual, and *nothing* in it is referenced by the metadata file. Either one
failing leaves the whole folder alone, on the same principle as drift --
guessing wrong about a user's files is worse than reporting nothing.

Only the middle two, plus legacy media, are cleanable, and only under
``--apply``. Drift is reported and never acted on: this tool does not decide
that a user's ROM is wrong.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .cache import Cache
from .dedupe import LibraryWriter
from .hasher import hash_file
from .scanner import ScannedSystem

__all__ = ["SystemReport", "VerifyReport", "clean_orphans", "verify_library"]

log = logging.getLogger(__name__)

# Folders no writer owns but no scraper put there either -- an emulator's own
# save data, never art from some earlier tool. Protected regardless of what
# any writer's layout looks like.
_PROTECTED_DIRS = frozenset({"saves", "states", "cheats", "patches", "dlc", "updates", "bios"})

# What "looks like art or a manual" means, for deciding a folder is safe to
# remove wholesale. Deliberately narrow: a folder holding anything outside
# this set is left alone rather than guessed about.
_LEGACY_MEDIA_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".tga",
        ".mp4",
        ".avi",
        ".mkv",
        ".mov",
        ".webm",
        ".mpg",
        ".mpeg",
        ".pdf",
        ".cbz",
        ".cbr",
    }
)


@dataclass
class SystemReport:
    """One system's consistency findings."""

    system: str
    system_dir: Path
    roms: int = 0
    drifted: list[tuple[Path, str, str]] = field(default_factory=list)
    """``(path, hash when last seen, hash now)``."""
    missing_roms: list[Path] = field(default_factory=list)
    orphan_media: list[Path] = field(default_factory=list)
    unlisted_roms: list[Path] = field(default_factory=list)
    unreadable: list[tuple[Path, str]] = field(default_factory=list)
    legacy_media: list[tuple[Path, list[Path]]] = field(default_factory=list)
    """``(folder, files in it)`` for each leftover-scraper folder found."""

    @property
    def clean(self) -> bool:
        return not (self.drifted or self.missing_roms or self.orphan_media or self.unreadable or self.legacy_media)

    @property
    def orphan_bytes(self) -> int:
        total = 0
        for path in self.orphan_media:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total


@dataclass
class VerifyReport:
    systems: list[SystemReport] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return all(report.clean for report in self.systems)

    @property
    def drifted(self) -> int:
        return sum(len(report.drifted) for report in self.systems)

    @property
    def missing_roms(self) -> int:
        return sum(len(report.missing_roms) for report in self.systems)

    @property
    def orphan_media(self) -> int:
        return sum(len(report.orphan_media) for report in self.systems)

    @property
    def unlisted_roms(self) -> int:
        return sum(len(report.unlisted_roms) for report in self.systems)

    @property
    def orphan_bytes(self) -> int:
        return sum(report.orphan_bytes for report in self.systems)

    @property
    def legacy_media(self) -> int:
        return sum(len(files) for report in self.systems for _folder, files in report.legacy_media)

    @property
    def legacy_bytes(self) -> int:
        total = 0
        for report in self.systems:
            for _folder, files in report.legacy_media:
                for path in files:
                    try:
                        total += path.stat().st_size
                    except OSError:
                        continue
        return total


def verify_library(
    systems: Sequence[ScannedSystem],
    *,
    cache: Cache | None = None,
    writer: LibraryWriter | None = None,
    rehash: bool = True,
    on_progress: Callable[[Path], None] | None = None,
) -> VerifyReport:
    """Re-check the library against the hash index and the metadata files.

    ``rehash`` is on by default and is the whole point: reading the cached hash
    back and comparing it to itself would verify nothing.
    """
    report = VerifyReport()

    for system in systems:
        if not system.is_known:
            continue
        entry = SystemReport(system=system.folder, system_dir=system.path, roms=len(system.roms))
        present = {path.resolve() for path in system.roms}
        stems = {path.stem for path in system.roms}

        if rehash and cache is not None:
            _check_drift(system, cache, entry, on_progress)

        if writer is not None:
            listed = writer.list_entries(system.path)
            entry.missing_roms = sorted(info.rom_path for info in listed if info.rom_path not in present)
            listed_paths = {info.rom_path for info in listed}
            entry.unlisted_roms = sorted(path for path in present if path not in listed_paths)
            entry.orphan_media = sorted(
                path for path, stem in writer.media_index(system.path).items() if stem not in stems
            )
            _find_legacy_media(system, writer, entry)

        report.systems.append(entry)

    return report


def _find_legacy_media(system: ScannedSystem, writer: LibraryWriter, entry: SystemReport) -> None:
    """Whole folders of art this writer doesn't own and the gamelist doesn't use.

    ``writer.media_index`` already accounts for this front-end's own layout
    (``images/``, ``videos/`` ...), so anything else sitting beside the ROMs
    is either an earlier scraper's leftovers or something the emulator itself
    owns (saves, cheats, patches).

    Two conditions, and a folder must clear both. Every file in it looks like
    art or a manual -- one unrecognised file and the folder is left alone
    rather than guessed about. And nothing in it is referenced by the
    metadata file: an entry this tool never rewrote can still point at the
    old scraper's folder, and art the front-end is displaying is in use no
    matter who wrote it.

    An empty folder clears both tests trivially and has no data to lose, so it
    counts as leftovers -- unless this system keeps its ROMs in subfolders. A
    library filed A/ to Z/ has an empty letter for the letter it owns nothing
    under yet, and that is structure, not residue.
    """
    try:
        referenced = writer.referenced_media(system.path)
    except Exception:  # unreadable metadata means "assume everything is in use"
        log.warning("%s: could not read media references - skipping legacy-media detection", system.folder)
        return

    root = system.path.resolve()
    filed_in_subfolders = any(rom.resolve().parent != root for rom in system.roms)

    known = writer.known_media_dirs() | _PROTECTED_DIRS
    for child in sorted(p for p in system.path.iterdir() if p.is_dir()):
        if child.name.startswith(".") or child.name.lower() in known:
            continue
        files = [f for f in child.rglob("*") if f.is_file() and not f.name.startswith(".")]
        if not files:
            if filed_in_subfolders:
                log.debug("%s: %s is empty but this system files ROMs in subfolders", system.folder, child.name)
                continue
            entry.legacy_media.append((child, []))
            continue
        if not all(f.suffix.lower() in _LEGACY_MEDIA_EXTENSIONS for f in files):
            continue
        if any(f.resolve() in referenced for f in files):
            log.debug("%s: %s is still referenced by the gamelist - leaving it", system.folder, child.name)
            continue
        entry.legacy_media.append((child, files))


def _check_drift(
    system: ScannedSystem,
    cache: Cache,
    entry: SystemReport,
    on_progress: Callable[[Path], None] | None,
) -> None:
    for rom_path in system.roms:
        if on_progress is not None:
            on_progress(rom_path)
        try:
            stat = rom_path.stat()
        except OSError as exc:
            entry.unreadable.append((rom_path, str(exc)))
            continue

        # `get_hashes` returns nothing when size or mtime moved, which is the
        # common case for a changed file -- so ask for the record directly and
        # compare content, not metadata.
        known = cache.get_hashes(rom_path, stat.st_size, stat.st_mtime)
        if known is not None:
            continue  # unchanged since it was hashed

        previous = _previous_md5(cache, rom_path)
        try:
            current = hash_file(rom_path)
        except OSError as exc:
            entry.unreadable.append((rom_path, str(exc)))
            continue

        if previous is not None and previous != current.md5:
            entry.drifted.append((rom_path, previous, current.md5))
        cache.put_hashes(rom_path, stat.st_size, stat.st_mtime, current)


def _previous_md5(cache: Cache, path: Path) -> str | None:
    record = cache.hash_record(path)
    return record.md5 if record is not None else None


def clean_orphans(
    report: VerifyReport,
    *,
    apply: bool,
    writer: LibraryWriter | None = None,
) -> tuple[int, int, list[str]]:
    """Remove orphan media, legacy-scraper folders, and dead metadata entries.

    Never touches a ROM: everything removed here is either regenerable by
    re-scraping or, for legacy media, was never this tool's output to begin
    with. Returns ``(media removed, entries removed, errors)``.
    """
    media_removed = 0
    entries_removed = 0
    errors: list[str] = []

    for system in report.systems:
        for path in system.orphan_media:
            if apply:
                try:
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
                    continue
            media_removed += 1

        for folder, files in system.legacy_media:
            if apply:
                try:
                    shutil.rmtree(folder)
                except OSError as exc:
                    errors.append(f"{folder}: {exc}")
                    continue
            media_removed += len(files)

        if system.missing_roms and writer is not None:
            if apply:
                entries_removed += writer.remove_entries(system.missing_roms, system.system_dir)
            else:
                entries_removed += len(system.missing_roms)

    return media_removed, entries_removed, errors
