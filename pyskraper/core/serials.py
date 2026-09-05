"""Disc images: what to hash, and the serial fallback when hashing cannot win.

Disc-based systems are where pure hash matching breaks down, and it is worth
being precise about why. ScreenScraper's disc hashes come from catalogues of
*original* images. Almost nobody stores those: ``.chd`` is a re-compression, and
a ``.cue``/``.bin`` rip may differ in track layout from the catalogued one. The
hash is then perfectly correct and matches nothing.

So disc systems get a second identifier: the **serial** stamped into the disc
itself (``SLUS_123.45`` and friends), which survives re-compression because it
lives in the filesystem on the disc rather than in the container around it.
This is how PS1 libraries actually get identified.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

__all__ = ["extract_serial", "resolve_disc_target"]

log = logging.getLogger(__name__)

# SLUS_123.45, SCES_003.11, SLPM_869.00 ... the executable name on the disc.
_SERIAL_RE = re.compile(rb"([A-Z]{4})[_-](\d{3})\.?(\d{2})")

# How far into the image to look. The PS1 boot config lives in the first few
# sectors; scanning further would mean reading gigabytes to find nothing.
_SCAN_BYTES = 4 * 1024 * 1024

# Containers we cannot read without decompressing them first.
_OPAQUE_SUFFIXES = frozenset({".chd", ".cso", ".pbp", ".rvz", ".wia"})


def resolve_disc_target(path: Path) -> Path:
    """Map a playlist or cue sheet to the file whose bytes actually matter.

    ``.m3u`` resolves to its first disc, matching what the front-end does when
    it lists a multi-disc game as one entry. ``.cue`` resolves to its first
    referenced track, because hashing the text of a cue sheet identifies the
    text file, not the game.
    """
    suffix = path.suffix.lower()

    if suffix == ".m3u":
        first = _first_referenced(path, extensions=None)
        return resolve_disc_target(first) if first is not None else path

    if suffix == ".cue":
        track = _first_referenced(path, extensions={".bin", ".img", ".iso"}, quoted=True)
        return track if track is not None else path

    return path


def _first_referenced(path: Path, *, extensions: set[str] | None, quoted: bool = False) -> Path | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        candidate = stripped
        if quoted:
            match = re.search(r'"([^"]+)"', stripped)
            if match:
                candidate = match.group(1)
            elif stripped.upper().startswith("FILE "):
                candidate = stripped.split(None, 1)[1].rsplit(None, 1)[0]
            else:
                continue

        resolved = (path.parent / candidate).resolve()
        if extensions is not None and resolved.suffix.lower() not in extensions:
            continue
        if resolved.exists():
            return resolved
    return None


def extract_serial(path: Path) -> str | None:
    """Read the disc serial out of an image, or ``None`` if it cannot be read.

    Returns ``None`` for compressed containers (``.chd``, ``.cso``, ``.pbp``)
    rather than guessing: reading those needs a full format decoder, and a wrong
    serial is worse than no serial -- it would confidently scrape the wrong game.
    """
    target = resolve_disc_target(path)

    if target.suffix.lower() in _OPAQUE_SUFFIXES:
        log.debug("%s is a compressed image; serial extraction needs a decoder", target.name)
        return None

    try:
        with open(target, "rb") as handle:
            head = handle.read(_SCAN_BYTES)
    except OSError as exc:
        log.debug("Could not read %s: %s", target.name, exc)
        return None

    match = _SERIAL_RE.search(head)
    if match is None:
        return None

    prefix = match.group(1).decode("ascii")
    return f"{prefix}-{match.group(2).decode('ascii')}{match.group(3).decode('ascii')}"
