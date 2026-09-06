"""The Batocera / KNULLI writer.

Media lands in per-type folders *inside* the system's ROM folder, named
``<rom stem>-<suffix>.<ext>`` -- the layout ``batocera-emulationstation``
produces itself, so a card scraped here is indistinguishable from one scraped
on the device.

The merge behaviour is the subtle part.  A run limited to one system, or to
five games, must not destroy the rest of the file, and it must never overwrite
the fields the handheld itself owns: ``<favorite>``, ``<playcount>``,
``<lastplayed>``, ``<gametime>``, ``<hidden>``, ``<emulator>`` and ``<core>``
are the user's play history and their emulator choices.  Clobbering those is
data loss that no amount of re-scraping can undo.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from ..core.atomic import atomic_text
from ..core.models import EntryInfo, ScrapeResult
from .base import register_writer

__all__ = ["PRESERVED_TAGS", "TAG_LAYOUT", "BatoceraWriter"]

log = logging.getLogger(__name__)

# gamelist tag -> (subfolder, filename suffix)
TAG_LAYOUT: dict[str, tuple[str, str]] = {
    "image": ("images", "-image"),
    "thumbnail": ("images", "-thumb"),
    "marquee": ("images", "-marquee"),
    "titleshot": ("images", "-titleshot"),
    "fanart": ("images", "-fanart"),
    "boxart": ("images", "-box"),
    "boxback": ("images", "-boxback"),
    "wheel": ("images", "-wheel"),
    "cartridge": ("images", "-cartridge"),
    "mix": ("images", "-mix"),
    "map": ("images", "-map"),
    "bezel": ("images", "-bezel"),
    "video": ("videos", "-video"),
    "manual": ("manuals", "-manual"),
    "magazine": ("magazines", "-magazine"),
}

# Written by the device, never by us.
PRESERVED_TAGS: tuple[str, ...] = (
    "favorite",
    "playcount",
    "lastplayed",
    "gametime",
    "hidden",
    "emulator",
    "core",
    "kidgame",
    "sortname",
)

# Order of metadata elements in each <game>, matching what the front-end emits.
_METADATA_ORDER: tuple[str, ...] = (
    "path",
    "name",
    "desc",
    "rating",
    "releasedate",
    "developer",
    "publisher",
    "genre",
    "family",
    "players",
    "region",
    "lang",
    "arcadesystemname",
)


class BatoceraWriter:
    """Writes ``<system>/gamelist.xml`` plus the media layout beside it."""

    name = "batocera"

    def __init__(self, *, merge: bool = True, write_hashes: bool = True, write_scraper_id: bool = True) -> None:
        self._merge = merge
        self._write_hashes = write_hashes
        self._write_scraper_id = write_scraper_id

    # ---- paths -----------------------------------------------------------

    def plan_paths(self, result: ScrapeResult, system_dir: Path) -> dict[str, Path]:
        """Target path for each of this game's resolved assets."""
        planned: dict[str, Path] = {}
        stem = result.rom.stem
        for asset in result.media:
            layout = TAG_LAYOUT.get(asset.tag)
            if layout is None:
                log.debug("No layout for media tag %r - skipping", asset.tag)
                continue
            folder, suffix = layout
            planned[asset.tag] = system_dir / folder / f"{stem}{suffix}{asset.extension}"
        return planned

    # ---- gamelist --------------------------------------------------------

    def gamelist_path(self, system_dir: Path) -> Path:
        return system_dir / "gamelist.xml"

    def write(self, results: list[ScrapeResult], system_dir: Path) -> Path:
        target = self.gamelist_path(system_dir)
        root = self._load_existing(target) if self._merge else ET.Element("gameList")

        existing = self._index_by_path(root)

        # Sorted, because `results` arrives in scrape-completion order -- which
        # is network timing, not anything about the library. Two runs over the
        # same card produced different XML, so every re-scrape looked like a
        # change to anything watching the file.
        by_path = sorted(((self._relative_rom_path(r, system_dir), r) for r in results), key=lambda pair: pair[0])
        for rom_path, result in by_path:
            element = existing.get(rom_path)
            if element is None:
                element = ET.SubElement(root, "game")
                ET.SubElement(element, "path").text = rom_path
                existing[rom_path] = element
            self._apply(element, result, system_dir)

        atomic_text(target, self._document(root))
        return target

    def _load_existing(self, target: Path) -> ET.Element:
        if not target.exists():
            return ET.Element("gameList")
        try:
            tree = ET.parse(target)
        except ET.ParseError as exc:
            # A corrupt gamelist is not a reason to lose the run, but it is a
            # reason to keep the original: back it up rather than overwrite it.
            backup = target.with_suffix(".xml.corrupt")
            log.warning("Existing gamelist is not valid XML (%s); preserving it as %s", exc, backup.name)
            target.replace(backup)
            return ET.Element("gameList")
        root = tree.getroot()
        return root if root.tag == "gameList" else ET.Element("gameList")

    # ---- reading back (dedupe / verify) ----------------------------------

    def list_entries(self, system_dir: Path) -> list[EntryInfo]:
        """What ``gamelist.xml`` currently lists, resolved to absolute paths."""
        target = self.gamelist_path(system_dir)
        if not target.exists():
            return []
        try:
            root = ET.parse(target).getroot()
        except ET.ParseError:
            # `verify` is a read-only diagnostic; a corrupt gamelist is
            # something to report, not a reason to abort the whole command.
            log.warning("Could not parse %s - treating it as empty", target)
            return []

        entries: list[EntryInfo] = []
        for game in root.findall("game"):
            path_node = game.find("path")
            if path_node is None or not path_node.text:
                continue
            raw = path_node.text.strip()
            entries.append(
                EntryInfo(
                    rom_path=(system_dir / raw).resolve(),
                    name=self._text(game, "name"),
                    game_id=self._int(self._text(game, "id")),
                )
            )
        return entries

    def media_index(self, system_dir: Path) -> dict[Path, str]:
        """Map every media file under this system back to the ROM stem it serves.

        The suffixes are matched longest-first so ``-boxback`` is never read as
        ``-box`` with a stray ``back`` left on the stem.
        """
        suffixes = sorted({suffix for _, suffix in TAG_LAYOUT.values()}, key=len, reverse=True)
        folders = {folder for folder, _ in TAG_LAYOUT.values()}

        index: dict[Path, str] = {}
        for folder in folders:
            directory = system_dir / folder
            if not directory.is_dir():
                continue
            for entry in directory.rglob("*"):
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                stem = entry.stem
                for suffix in suffixes:
                    if stem.endswith(suffix):
                        index[entry] = stem[: -len(suffix)]
                        break
        return index

    def known_media_dirs(self) -> frozenset[str]:
        return frozenset(folder for folder, _ in TAG_LAYOUT.values())

    def referenced_media(self, system_dir: Path) -> set[Path]:
        """Resolve every media tag in the gamelist to an absolute path.

        Deliberately reads the tag's text rather than reconstructing the path
        from `TAG_LAYOUT`: an entry written by a different scraper points
        wherever it points, and the whole reason to ask is to find out where.
        """
        target = self.gamelist_path(system_dir)
        if not target.exists():
            return set()
        try:
            root = ET.parse(target).getroot()
        except ET.ParseError:
            # Same stance as `list_entries`: a corrupt gamelist is reported by
            # the caller, not a reason to claim nothing is referenced -- which
            # here would read as "safe to delete".
            log.warning("Could not parse %s - treating every media file as in use", target)
            raise

        referenced: set[Path] = set()
        for game in root.findall("game"):
            for tag in TAG_LAYOUT:
                node = game.find(tag)
                if node is not None and node.text and node.text.strip():
                    referenced.add((system_dir / node.text.strip()).resolve())
        return referenced

    def remove_entries(self, rom_paths: list[Path], system_dir: Path) -> int:
        """Drop these ROMs' ``<game>`` elements. Leaves every other entry alone."""
        target = self.gamelist_path(system_dir)
        if not target.exists() or not rom_paths:
            return 0
        try:
            tree = ET.parse(target)
        except ET.ParseError:
            log.warning("Refusing to edit unparseable gamelist %s", target)
            return 0

        root = tree.getroot()
        wanted = {path.resolve() for path in rom_paths}
        removed = 0
        for game in list(root.findall("game")):
            path_node = game.find("path")
            if path_node is None or not path_node.text:
                continue
            if (system_dir / path_node.text.strip()).resolve() in wanted:
                root.remove(game)
                removed += 1

        if removed:
            atomic_text(target, self._document(root))
        return removed

    @staticmethod
    def _text(element: ET.Element, tag: str) -> str | None:
        node = element.find(tag)
        return node.text.strip() if node is not None and node.text else None

    @staticmethod
    def _int(value: str | None) -> int | None:
        try:
            return int(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def _index_by_path(root: ET.Element) -> dict[str, ET.Element]:
        index: dict[str, ET.Element] = {}
        for game in root.findall("game"):
            path_node = game.find("path")
            if path_node is not None and path_node.text:
                index[path_node.text.strip()] = game
        return index

    @staticmethod
    def _relative_rom_path(result: ScrapeResult, system_dir: Path) -> str:
        try:
            relative = result.rom.path.relative_to(system_dir)
        except ValueError:
            return result.rom.name
        return f"./{relative.as_posix()}"

    def _apply(self, element: ET.Element, result: ScrapeResult, system_dir: Path) -> None:
        """Update one <game>, preserving the fields the device owns."""
        preserved = {tag: node for tag in PRESERVED_TAGS if (node := element.find(tag)) is not None}

        metadata = result.metadata
        values: dict[str, str | None] = {
            "name": metadata.name,
            "desc": metadata.description,
            "rating": f"{metadata.rating:.4f}".rstrip("0").rstrip(".") if metadata.rating is not None else None,
            "releasedate": metadata.release_date,
            "developer": metadata.developer,
            "publisher": metadata.publisher,
            "genre": metadata.genre,
            "family": metadata.family,
            "players": metadata.players,
            "region": metadata.region,
            "lang": metadata.language,
            "arcadesystemname": metadata.arcade_system,
        }

        for tag, value in values.items():
            self._set(element, tag, value)

        for tag, path in self.plan_paths(result, system_dir).items():
            asset = result.asset(tag)
            if asset is None or not asset.downloaded:
                continue
            try:
                relative = path.relative_to(system_dir).as_posix()
            except ValueError:
                continue
            self._set(element, tag, f"./{relative}")

        if self._write_hashes and result.rom.hashes is not None:
            self._set(element, "md5", result.rom.hashes.md5)
            self._set(element, "crc32", result.rom.hashes.crc32)
        if self._write_scraper_id and metadata.ss_game_id is not None:
            self._set(element, "id", str(metadata.ss_game_id))
            self._set(element, "source", "ScreenScraper.fr")

        # Re-attach the device's own fields, which are never in `values`.
        for tag, node in preserved.items():
            if element.find(tag) is None:
                element.append(node)

        self._reorder(element)

    @staticmethod
    def _set(element: ET.Element, tag: str, value: str | None) -> None:
        if value is None or value == "":
            return
        node = element.find(tag)
        if node is None:
            node = ET.SubElement(element, tag)
        node.text = value

    @staticmethod
    def _reorder(element: ET.Element) -> None:
        """Sort children into a stable order so diffs between runs stay readable."""
        order = {tag: i for i, tag in enumerate(_METADATA_ORDER)}
        media_order = {tag: 100 + i for i, tag in enumerate(TAG_LAYOUT)}
        order.update(media_order)
        tail = {tag: 200 + i for i, tag in enumerate(("md5", "crc32", "id", "source", *PRESERVED_TAGS))}
        order.update(tail)

        children = list(element)
        children.sort(key=lambda node: order.get(node.tag, 500))
        element[:] = children

    @classmethod
    def _document(cls, root: ET.Element) -> str:
        """The complete gamelist as text: indented, declared, newline-terminated.

        Both writing paths need exactly this, and assembling it twice is how the
        declaration or the trailing newline ends up on one and not the other.
        """
        cls._indent(root)
        return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"

    @staticmethod
    def _indent(element: ET.Element, level: int = 0) -> None:
        pad = "\n" + "  " * level
        if len(element):
            if not (element.text or "").strip():
                element.text = pad + "  "
            for child in element:
                BatoceraWriter._indent(child, level + 1)
            last = element[-1]
            if not (last.tail or "").strip():
                last.tail = pad
        if level and not (element.tail or "").strip():
            element.tail = pad


register_writer("batocera", BatoceraWriter)
