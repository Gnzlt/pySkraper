"""Reading a card to find out which handheld it came out of.

KNULLI stamps a one-line board name onto the boot partition when the image is
built, and copies it to the userdata partition on every boot, so a mounted card
already knows what it is.  This module reads that name and maps it to a device
profile, which is how the wizard can offer the right device instead of guessing.

Three things are worth knowing about the sources.

**The canonical file is on the boot partition** (``/boot/boot/knulli.board``),
and on macOS it is frequently unreadable: an msdos volume mounted by fskit
answers ``EPERM`` to every read, so ``KNULLI/boot/knulli.board`` fails even
though the file is plainly there.  It is still tried first, because it works on
Linux and it is the only source that survives a user pointing ``sharedevice=``
at a different disk.

**The mirror on the userdata partition is what actually carries this feature.**
KNULLI's ``S21batteryplus-daemon`` copies the boot file to
``system/configs/batteryplus/knulli.board`` at every boot, and re-copies it
whenever the two differ, so it cannot go stale against the hardware.

**Everything is best-effort.**  A missing file, an unreadable mount, a board
this version has never heard of -- all of them mean "not detected", never an
error.  The profile only supplies a default artwork size, so the cost of not
knowing is that the user answers the question themselves, exactly as before.
"""

from __future__ import annotations

import re
from pathlib import Path

from .devices import PROFILES, DeviceProfile

__all__ = [
    "BOARD_TO_PROFILES",
    "board_sources",
    "detect_profiles",
    "read_board",
]

# Board names are the directory names under `board/<soc>/` in
# knulli-cfw/knulli-linux, which is also what ends up in `knulli.board`.
#
# The mapping is one board to one profile everywhere except `rg35xx-plus`:
# KNULLI ships a single image for the RG35XX 2024 and the RG35XX Plus, so the
# board name genuinely cannot separate them.  Both are 640x480, so the artwork
# size is unambiguous even when the model is not -- the value is a tuple, the
# first entry is what gets offered, and the wizard says the sibling shares it.
#
# `sm8250` is deliberately absent.  A freshly flashed Retroid card carries the
# SoC name until first boot, when `rcS` reads the device tree and rewrites the
# file as `rp5`/`rpflip2`/`rpmini`/`rpminiv2`.  Before that it identifies four
# devices with three different screen sizes, so the honest answer is no answer.
BOARD_TO_PROFILES: dict[str, tuple[str, ...]] = {
    "rg35xx-plus": ("anbernic-rg35xx-2024", "anbernic-rg35xx-plus"),
    "rg35xx-h": ("anbernic-rg35xx-h",),
    "rg35xx-sp": ("anbernic-rg35xx-sp",),
    "rg35xx-pro": ("anbernic-rg35xx-pro",),
    "rg35xx": ("anbernic-rg35xx",),
    "rg28xx": ("anbernic-rg28xx",),
    "rg34xx": ("anbernic-rg34xx",),
    "rg34xx-sp": ("anbernic-rg34xx-sp",),
    "rg40xx-h": ("anbernic-rg40xx-h",),
    "rg40xx-v": ("anbernic-rg40xx-v",),
    "rg-cubexx": ("anbernic-rgcubexx",),
    "rg-arc-s": ("anbernic-rg-arc-s",),
    "trimui-brick": ("trimui-brick",),
    "trimui-smart-pro": ("trimui-smart-pro",),
    "trimui-smart-pro-s": ("trimui-smart-pro-s",),
    "miyoo-flip": ("miyoo-flip",),
    "powkiddy-rgb30": ("powkiddy-rgb30",),
    "powkiddy-x55": ("powkiddy-x55",),
    "powkiddy-v20": ("powkiddy-v20",),
    "powkiddy-v90s": ("powkiddy-v90s",),
    "powkiddy_a13": ("powkiddy-a13",),
    "magicx-mini-m": ("magicx-xu-mini-m",),
    "pixel2": ("gkd-pixel-2",),
    "g350": ("batlexp-g350",),
    "r36s": ("r36s",),
    "ps5000": ("ps5000",),
    "ps7000": ("ps7000",),
    "rp5": ("retroid-pocket-5",),
    "rpflip2": ("retroid-pocket-flip-2",),
    "rpmini": ("retroid-pocket-mini",),
    "rpminiv2": ("retroid-pocket-mini-v2",),
    "orangepi-zero2": ("orange-pi-zero-2w",),
}

# Board names are short lowercase tokens.  Anything else came from a file that
# is not what we thought it was, and guessing from it would be worse than
# admitting we do not know.
_BOARD = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

# The log line the KNULLI updater writes, e.g. "Board: rg35xx-plus".
_LOG_LINE = re.compile(r"^Board:\s*(\S+)\s*$", re.MULTILINE)

# Enough for the one-line board file many times over, and small enough that
# pointing this at a wrong file costs nothing.  The log is read whole but is
# only ever a few hundred bytes.
_MAX_READ = 8192


def board_sources(volume: Path, boot: Path | None = None) -> list[Path]:
    """Where a board name might be found, canonical first.

    Split out from :func:`read_board` so the search order can be asserted
    directly rather than inferred from which file happened to win.
    """
    sources = []
    if boot is not None:
        sources.append(boot / "boot" / "knulli.board")
    sources.append(volume / "system" / "configs" / "batteryplus" / "knulli.board")
    sources.append(volume / "system" / "logs" / "knulli.log")
    return sources


def _read(path: Path) -> str | None:
    """The head of ``path``, or ``None`` if it cannot be read.

    Unreadable is not exceptional here: the boot partition routinely refuses
    reads on macOS, and a card can be pulled between one call and the next.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(_MAX_READ).decode("utf-8", "replace")
    except OSError:
        return None


def _parse(path: Path, text: str) -> str | None:
    """The board name in ``text``, given that it came from ``path``."""
    if path.suffix == ".log":
        found = _LOG_LINE.findall(text)
        # The updater appends, so the last line is the current hardware.
        candidate = found[-1] if found else ""
    else:
        candidate = text.strip().splitlines()[0].strip() if text.strip() else ""
    return candidate if _BOARD.match(candidate) else None


def read_board(volume: Path, boot: Path | None = None) -> str | None:
    """The KNULLI board name for this card, or ``None`` if it does not say.

    ``volume`` is the partition holding ``roms/`` and ``system/``; ``boot`` is
    the boot partition when one was found beside it.
    """
    for source in board_sources(volume, boot):
        text = _read(source)
        if text is None:
            continue
        board = _parse(source, text)
        if board is not None:
            return board
    return None


def detect_profiles(volume: Path, boot: Path | None = None) -> tuple[DeviceProfile, ...]:
    """The profiles this card's board could be, best first.

    Empty when the card says nothing, or when it names a board newer than this
    version of the mapping -- both of which mean "ask the user", not "fail".
    """
    board = read_board(volume, boot)
    if board is None:
        return ()
    return tuple(PROFILES[name] for name in BOARD_TO_PROFILES.get(board, ()) if name in PROFILES)
