"""The boundary types between "fetch and cache" and "generate output".

``ScrapeResult`` is the only thing writers see.  It is deliberately
front-end-agnostic and carries no ScreenScraper vocabulary, so adding an
output format never means touching the API layer -- and a writer can be tested
against a hand-built result with no network and no fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..systems import SystemInfo
from .hasher import Hashes

__all__ = ["EntryInfo", "GameMetadata", "MediaAsset", "ResolutionMethod", "RomFile", "ScrapeResult"]


class ResolutionMethod(StrEnum):
    """How a ROM was matched.  Surfaced per game in the log and the run summary,
    so the user can see hashing working rather than take it on faith."""

    HASH = "hash"
    SERIAL = "serial"
    FILENAME = "filename"
    SEARCH = "search"
    CACHE = "cache"
    UNMATCHED = "unmatched"


@dataclass
class RomFile:
    """One candidate game file on the card."""

    path: Path
    system: SystemInfo
    size: int
    hashes: Hashes | None = None

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class MediaAsset:
    """One downloadable asset, resolved to a concrete URL and target path."""

    tag: str
    key: str
    url: str
    region: str | None = None
    fmt: str | None = None
    expected_size: int | None = None
    md5: str | None = None
    target: Path | None = None
    downloaded: bool = False

    @property
    def extension(self) -> str:
        if self.fmt:
            return f".{self.fmt.lower().lstrip('.')}"
        return ".png"


@dataclass
class GameMetadata:
    """Front-end-agnostic game facts."""

    name: str
    ss_game_id: int | None = None
    description: str | None = None
    genre: str | None = None
    developer: str | None = None
    publisher: str | None = None
    release_date: str | None = None
    players: str | None = None
    rating: float | None = None
    region: str | None = None
    language: str | None = None
    family: str | None = None
    arcade_system: str | None = None


@dataclass
class ScrapeResult:
    """Everything a writer needs about one game."""

    rom: RomFile
    metadata: GameMetadata
    media: list[MediaAsset] = field(default_factory=list)
    method: ResolutionMethod = ResolutionMethod.UNMATCHED

    @property
    def matched(self) -> bool:
        return self.method is not ResolutionMethod.UNMATCHED

    def asset(self, tag: str) -> MediaAsset | None:
        for item in self.media:
            if item.tag == tag:
                return item
        return None


@dataclass(frozen=True)
class EntryInfo:
    """One game as a front-end's metadata file currently has it.

    The read-back counterpart to :class:`ScrapeResult`: where that says what a
    writer should put on the card, this says what is already there.  ``dedupe``
    and ``verify`` work in this direction, and they must do it without knowing
    whether the source was XML, a text file or JSON.
    """

    rom_path: Path
    """Absolute, resolved against the system directory."""
    name: str | None = None
    game_id: int | None = None
