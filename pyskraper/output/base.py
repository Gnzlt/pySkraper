"""The writer boundary.

Rule 1 of this project: KNULLI/Batocera is the only front-end tuned to the
byte, and every other front-end is a ``Writer`` implementation and nothing
else.  No front-end-specific branching is permitted outside this package -- if
something elsewhere needs to know the output format, the protocol is wrong and
gets extended rather than special-cased.

Writers read only from resolved :class:`ScrapeResult` objects, never from the
API, which is what makes re-generating a library in a different format cost
zero requests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..core.models import EntryInfo, ScrapeResult

__all__ = ["EntryInfo", "Writer", "get_writer", "register_writer"]


@runtime_checkable
class Writer(Protocol):
    """What every output format must provide.

    The first two methods are the scrape path.  The last three are the hygiene
    path (``dedupe``, ``verify``): reading back what is already on the card and
    removing entries for ROMs that are gone.  They live here rather than in
    ``core/`` because *where the media sits* and *what the metadata file looks
    like* are precisely the things rule 1 says only a writer may know.
    """

    name: str

    def plan_paths(self, result: ScrapeResult, system_dir: Path) -> dict[str, Path]:
        """Where each of this game's assets should land."""
        ...

    def write(self, results: list[ScrapeResult], system_dir: Path) -> Path:
        """Write the metadata file for one system.  Returns the path written."""
        ...

    def list_entries(self, system_dir: Path) -> list[EntryInfo]:
        """Read back the games this system's metadata file currently lists.

        Returns an empty list when there is no metadata file yet, rather than
        raising -- an unscraped system is a normal state, not an error.
        """
        ...

    def media_index(self, system_dir: Path) -> dict[Path, str]:
        """Every media file this writer owns under ``system_dir``, mapped to the
        ROM stem it belongs to.

        One walk per system rather than a glob per ROM: a 3,000-ROM system
        directory makes that difference minutes.
        """
        ...

    def remove_entries(self, rom_paths: list[Path], system_dir: Path) -> int:
        """Drop these ROMs from the metadata file.  Returns how many went.

        Never touches the ROM or its media -- the caller owns those, and owns
        the safety discipline around removing them.
        """
        ...


_REGISTRY: dict[str, type] = {}


def register_writer(name: str, writer_class: type) -> None:
    _REGISTRY[name] = writer_class


def get_writer(name: str, **kwargs: object) -> Writer:
    try:
        writer_class = _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(f"Unknown output format {name!r}. Available: {known}") from None
    instance = writer_class(**kwargs)
    return instance  # type: ignore[no-any-return]
