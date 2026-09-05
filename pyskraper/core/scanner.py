"""Walk the ROM tree.

The layout is Batocera's: ``<roms>/<system>/<rom files>``, with media living in
subfolders of each system directory.  Those media folders are the main hazard --
walking them would try to scrape thumbnails as if they were games.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..systems import SystemInfo, lookup

__all__ = ["MEDIA_DIRS", "ScannedSystem", "scan_system", "scan_tree"]

log = logging.getLogger(__name__)

# Media and front-end scratch directories that live inside a system folder.
MEDIA_DIRS = frozenset(
    {
        "images",
        "videos",
        "manuals",
        "magazines",
        "media",
        "downloaded_media",
        "covers",
        "screenshots",
        "wheels",
        "marquees",
        "box",
        "boxart",
        "bezels",
        "cheats",
        "saves",
        "states",
        "patches",
        "dlc",
        "updates",
    }
)

# Files that are never games, whatever their extension.
SKIP_FILES = frozenset({"gamelist.xml", "gamelist.xml.bak", "systeminfo.txt", "metadata.txt", "desktop.ini"})

# Extensions that are never games even inside a system folder.
SKIP_EXTENSIONS = frozenset({".xml", ".txt", ".srm", ".sav", ".state", ".cfg", ".dat", ".db", ".part", ".nfo"})


@dataclass
class ScannedSystem:
    """One system directory on the card."""

    folder: str
    path: Path
    info: SystemInfo | None
    roms: list[Path] = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return self.info is not None

    @property
    def systeme_id(self) -> int | None:
        return self.info.systeme_id if self.info else None


def _is_rom_candidate(path: Path, info: SystemInfo | None) -> bool:
    if not path.is_file():
        return False
    name = path.name
    if name.startswith("."):
        return False
    if name.lower() in SKIP_FILES:
        return False

    suffix = path.suffix.lower()
    if suffix in SKIP_EXTENSIONS:
        return False

    if info is not None and info.extensions:
        # A .bin beside a .cue is a track, not a game; the .cue is the entry
        # point the front-end lists, so bare .bin files are skipped when a
        # sibling .cue or .m3u references them.
        if suffix == ".bin" and _has_sibling_playlist(path):
            return False
        return suffix in info.extensions

    return True


def _has_sibling_playlist(path: Path) -> bool:
    stem = path.stem.lower()
    for sibling in path.parent.glob("*"):
        if sibling.suffix.lower() in (".cue", ".m3u", ".gdi", ".ccd") and sibling.stem.lower() == stem:
            return True
    return False


def scan_system(system_dir: Path) -> ScannedSystem:
    """List the ROMs in one system directory."""
    folder = system_dir.name
    info = lookup(folder)
    scanned = ScannedSystem(folder=folder, path=system_dir, info=info)

    if info is None:
        # Debug, not warning: a stock KNULLI card ships ~120 folders we do not
        # map, and warning per folder buries every real message. `pyskraper
        # systems` and `pyskraper doctor` both report the unmapped set explicitly.
        log.debug("Unknown system folder %r - skipping", folder)
        return scanned

    for entry in sorted(system_dir.rglob("*")):
        if entry.is_dir():
            continue
        # Skip anything under a media directory at any depth.
        relative_parts = {part.lower() for part in entry.relative_to(system_dir).parts[:-1]}
        if relative_parts & MEDIA_DIRS:
            continue
        if any(part.startswith(".") for part in entry.relative_to(system_dir).parts):
            continue
        if _is_rom_candidate(entry, info):
            scanned.roms.append(entry)

    return scanned


def scan_tree(
    roms_root: Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[ScannedSystem]:
    """Scan every system directory under ``roms_root``.

    ``include`` wins when non-empty: an explicit include list means "only
    these", and ``exclude`` then filters within it.
    """
    include_set = {s.strip().lower() for s in (include or []) if s.strip()}
    exclude_set = {s.strip().lower() for s in (exclude or []) if s.strip()}

    results: list[ScannedSystem] = []
    for entry in sorted(roms_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        folder = entry.name.lower()
        if folder in MEDIA_DIRS:
            continue
        if include_set and folder not in include_set:
            continue
        if folder in exclude_set:
            log.debug("Skipping excluded system %r", folder)
            continue
        results.append(scan_system(entry))

    return results
