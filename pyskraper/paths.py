"""Where pySkraper keeps its things, and where it looks for SD cards.

Everything the tool writes -- config, cache database, resume journals, logs,
dedupe journals -- lives under a single :func:`base_dir`.  Nothing is ever
written to ``~/.config``, ``~/Library`` or any other system location, so the
whole folder can be copied to a USB stick and carried to another machine
alongside the card reader.

The other half of this module is the opposite problem: finding a card that
someone *else* mounted.  That is the one place where the operating system's
conventions unavoidably leak in, so they are collected here rather than
scattered through the wizard.
"""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
from pathlib import Path

__all__ = [
    "CACHE_DB_FILENAME",
    "CONFIG_FILENAME",
    "base_dir",
    "boot_partition_beside",
    "cache_dir",
    "cache_path",
    "config_path",
    "is_boot_volume",
    "is_writable",
    "mount_roots",
    "set_base_dir",
]

CONFIG_FILENAME = "pyskraper.yaml"
CACHE_DB_FILENAME = "pyskraper.db"
_DATA_DIRNAME = "data"

# Set by ``--data-dir``.  A module-level override rather than a parameter
# threaded through every call site: the base directory is process-wide by
# nature, and passing it everywhere would put a path argument on functions that
# have no other reason to take one.
_override: Path | None = None


def set_base_dir(path: Path | str | None) -> None:
    """Point every subsequent lookup at ``path``.  ``None`` restores discovery."""
    global _override
    _override = Path(path).expanduser().resolve() if path is not None else None


def _project_root() -> Path:
    """The folder containing the package -- i.e. the folder you copied."""
    return Path(__file__).resolve().parent.parent


def is_writable(directory: Path) -> bool:
    """Probe by actually writing.

    ``os.access(W_OK)`` is the obvious call and it is unreliable: it answers
    from the permission bits, which say nothing about a read-only mount, and on
    some filesystems it disagrees with what ``open()`` will do.  Since being
    wrong here means discovering it later, halfway through a scrape, the probe
    is worth the two syscalls.
    """
    if not directory.is_dir():
        return False
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".pyskraper-probe-"):
            return True
    except OSError:
        return False


def base_dir() -> Path:
    """The directory holding this installation's config and data.

    Resolution order:

    1. ``--data-dir`` (via :func:`set_base_dir`)
    2. ``$PYSKRAPER_HOME``
    3. the project root, when it is writable -- the portable case, where the
       package sits inside the folder the user copied
    4. the current working directory

    Step 4 exists for ``pip install``: an installed package lives in
    ``site-packages``, which is not writable and must not be written to even
    when it is.  Falling back to the working directory keeps an installed copy
    usable without ever polluting the environment.
    """
    if _override is not None:
        return _override

    env = os.environ.get("PYSKRAPER_HOME")
    if env:
        return Path(env).expanduser().resolve()

    root = _project_root()
    if is_writable(root) and not _looks_installed(root):
        return root

    return Path.cwd().resolve()


def _looks_installed(root: Path) -> bool:
    """True when the package was installed rather than copied.

    An editable install leaves the package in the project folder, which is the
    portable case and must keep working.  A regular install puts it under
    ``site-packages`` or ``dist-packages``, where writing would leak state into
    the Python environment and survive an uninstall.
    """
    return any(part in ("site-packages", "dist-packages") for part in root.parts)


def config_path() -> Path:
    return base_dir() / CONFIG_FILENAME


def cache_dir() -> Path:
    return base_dir() / _DATA_DIRNAME


def cache_path(directory: Path) -> Path:
    """The cache database inside ``directory``.

    Takes the directory rather than reading `cache_dir()` itself, because the
    caller's `config.paths.cache` may have been overridden and must win.
    """
    return Path(directory) / CACHE_DB_FILENAME


# --------------------------------------------------------------------------
# Finding a card
# --------------------------------------------------------------------------

# Volume names that indicate a handheld's boot partition sitting beside its data
# partition.  Observed on the real card: /Volumes/SHARE holds roms/, and
# /Volumes/KNULLI is the boot partition mounted separately.  The same sibling
# relationship holds under a Linux mount root.
BOOT_VOLUME_NAMES = frozenset({"knulli", "batocera", "recalbox", "arkos", "muos"})


def is_boot_volume(path: Path) -> bool:
    """True when ``path`` is named like a handheld's boot partition."""
    return path.name.lower() in BOOT_VOLUME_NAMES


def boot_partition_beside(volume: Path) -> Path | None:
    """The boot partition mounted next to ``volume``, if there is one.

    For callers that already hold the list of mounted volumes, matching against
    :func:`is_boot_volume` directly avoids the extra directory read this does.
    """
    try:
        siblings = list(volume.parent.iterdir())
    except OSError:
        return None
    return next((s for s in siblings if s != volume and is_boot_volume(s)), None)


def candidate_mount_roots(platform: str | None = None) -> list[Path]:
    """Where this platform *would* mount removable volumes, existence aside.

    Split from :func:`mount_roots` so the per-platform policy can be checked
    without depending on which directories happen to exist on the machine
    running the tests -- otherwise the Linux branch is untestable on a Mac.
    """
    platform = platform if platform is not None else sys.platform
    candidates: list[Path] = []

    if platform == "darwin":
        candidates.append(Path("/Volumes"))
    elif platform.startswith("linux"):
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            # No passwd entry -- containers and some CI images.  The per-user
            # media directories simply do not apply.
            user = ""
        if user:
            candidates.append(Path("/run/media") / user)
            candidates.append(Path("/media") / user)
        candidates.append(Path("/media"))
        candidates.append(Path("/mnt"))

    return candidates


def mount_roots() -> list[Path]:
    """The candidate roots that actually exist, in preference order.

    An empty list is a meaningful answer: it means this platform's conventions
    found nothing, and the caller should ask the user to type a path rather
    than guess.
    """
    seen: set[Path] = set()
    roots: list[Path] = []
    for candidate in candidate_mount_roots():
        if candidate in seen or not candidate.is_dir():
            continue
        seen.add(candidate)
        roots.append(candidate)
    return roots
