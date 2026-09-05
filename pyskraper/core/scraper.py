"""Run orchestration: plan, execute, write.

The split this file maintains is the one the whole design rests on -- fetching
and resolving happens here, and *nothing* here knows what a gamelist looks
like.  Writers are handed finished :class:`ScrapeResult` objects at the end of
each system.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..api.client import ScreenScraperClient
from ..api.errors import QuotaExceededError, ScreenScraperError
from ..api.parser import parse_media, parse_metadata
from ..api.selectors import build_region_chain
from ..config import Config
from ..exits import EXIT_OK, EXIT_PARTIAL, EXIT_QUOTA
from ..output.base import Writer
from .cache import Cache
from .identify import Identifier
from .images import apply_server_resize, resize_if_needed
from .journal import RunJournal
from .media import MediaDownloader
from .models import MediaAsset, ResolutionMethod, RomFile, ScrapeResult
from .scanner import ScannedSystem

__all__ = ["ProgressEvent", "RunStats", "Scraper"]

log = logging.getLogger(__name__)

# How many resolved games may sit in memory before the gamelist is written.
# Small enough that a hard kill (power loss, a card yanked mid-run) costs
# minutes rather than hours; large enough that a big system is not rewriting
# a megabyte of XML every few games.
CHECKPOINT_EVERY = 200


@dataclass
class RunStats:
    scanned: int = 0
    matched: int = 0
    unmatched: int = 0
    failed: int = 0
    media_downloaded: int = 0
    media_skipped: int = 0
    media_failed: int = 0
    media_resized: int = 0
    bytes_downloaded: int = 0
    media_planned: int = 0
    bytes_planned: int = 0
    """What a real run *would* fetch. Populated on dry runs from the sizes the
    API reports alongside each asset, so a cost estimate needs no downloads."""
    api_requests: int = 0
    bytes_by_tag: Counter[str] = field(default_factory=Counter)
    """Per-tag byte totals. Turns "trim your media config" into a specific list."""
    by_method: Counter[str] = field(default_factory=Counter)
    stopped_early: bool = False
    resumed: int = 0
    """ROMs skipped because an earlier interrupted run already finished them."""
    unmatched_files: list[Path] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.stopped_early:
            return EXIT_QUOTA
        if self.failed or self.unmatched:
            return EXIT_PARTIAL
        return EXIT_OK


@dataclass
class ProgressEvent:
    system: str
    rom: Path
    result: ScrapeResult | None
    error: str | None = None

    @property
    def method(self) -> str:
        if self.error:
            return "error"
        return str(self.result.method) if self.result else "unmatched"


ProgressHook = Callable[[ProgressEvent], None]


class Scraper:
    def __init__(
        self,
        config: Config,
        client: ScreenScraperClient,
        writer: Writer,
        *,
        dry_run: bool = False,
        limit: int | None = None,
        on_progress: ProgressHook | None = None,
        cache: Cache | None = None,
        journal: RunJournal | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._writer = writer
        self._cache = cache
        self._journal = journal
        self._identifier = Identifier(client, config, cache)
        self._dry_run = dry_run
        self._limit = limit
        self._on_progress = on_progress
        self.stats = RunStats()
        self._pending: list[tuple[Path, str, str, bool]] = []
        """Journal records awaiting a gamelist write -- see `_flush_journal`."""

        prefs = config.preferences
        self._region_chain = build_region_chain(prefs.language, prefs.region, prefs.region_fallback)
        self._language_chain = build_region_chain(prefs.language, None, ["en", "wor"])
        self._downloader = MediaDownloader(
            client.http,
            concurrency=max(2, client.governor.concurrency),
            overwrite=config.output.overwrite,
        )
        self._stop = asyncio.Event()

    async def run(self, systems: list[ScannedSystem]) -> RunStats:
        for system in systems:
            if self._stop.is_set():
                break
            if not system.is_known:
                continue
            await self._run_system(system)
        return self.stats

    async def _run_system(self, system: ScannedSystem) -> None:
        roms = system.roms[: self._limit] if self._limit else system.roms
        if self._journal is not None:
            before = len(roms)
            roms = [r for r in roms if r not in self._journal]
            skipped = before - len(roms)
            if skipped:
                self.stats.resumed += skipped
                log.info("%s: skipping %d ROM(s) already done in an earlier run", system.folder, skipped)
        if not roms:
            return

        log.info("Scraping %s (%d ROMs)", system.folder, len(roms))
        results: list[ScrapeResult] = []
        written_through = 0
        concurrency = max(1, self._client.governor.concurrency)
        queue: asyncio.Queue[Path] = asyncio.Queue()
        for path in roms:
            queue.put_nowait(path)

        def checkpoint() -> None:
            """Write what is resolved so far, then journal it as done.

            The ordering is the whole point.  A ROM is recorded in the journal
            only once its `<game>` entry is on disk, because the journal is
            what makes the next run skip it.  Recording first -- which is what
            this used to do -- meant an interrupt threw away a system's
            metadata while the journal still claimed the work was finished, and
            no later run would ever go back for it.  The media was on the card;
            the entry describing it was not.
            """
            nonlocal written_through
            pending = results[written_through:]
            if pending and not self._dry_run:
                target = self._writer.write(pending, system.path)
                log.info("Wrote %s (%d games)", target, len(pending))
            written_through = len(results)
            self._flush_journal()

        async def worker() -> None:
            while not self._stop.is_set():
                try:
                    path = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await self._process_rom(path, system)
                    if result is not None:
                        results.append(result)
                        if len(results) - written_through >= CHECKPOINT_EVERY:
                            # On the loop, not in a thread: `checkpoint` reads
                            # `results` and advances `written_through`, and a
                            # worker appending in between would mark a game
                            # written that never was. One XML write costs far
                            # less than the 200 round-trips that earned it.
                            checkpoint()
                finally:
                    queue.task_done()

        try:
            await asyncio.gather(*(worker() for _ in range(concurrency)))
        finally:
            # Quota, Ctrl-C, a fatal API error, a pulled card: every one of
            # them ends the run here, and every one of them must still leave
            # the games it already resolved on the card.  Synchronous on
            # purpose -- an `await` in this path can itself be cancelled.
            checkpoint()

    async def _process_rom(self, path: Path, system: ScannedSystem) -> ScrapeResult | None:
        if system.info is None:  # pragma: no cover - unknown systems are filtered before here
            raise RuntimeError(f"{system.path}: cannot scrape a system with no ScreenScraper mapping")
        self.stats.scanned += 1

        try:
            size = path.stat().st_size
        except OSError as exc:
            self._fail(system, path, f"unreadable: {exc}")
            return None

        rom = RomFile(path=path, system=system.info, size=size)

        try:
            identification = await self._identifier.identify(rom)
        except QuotaExceededError as exc:
            log.warning("%s", exc)
            self.stats.stopped_early = True
            self._stop.set()
            return None
        except ScreenScraperError as exc:
            if exc.fatal:
                self._stop.set()
                raise
            self._fail(system, path, f"{type(exc).__name__}: {exc}")
            return None

        game = identification.game
        method = identification.method

        if game is None:
            self.stats.unmatched += 1
            self.stats.unmatched_files.append(path)
            self.stats.by_method[str(ResolutionMethod.UNMATCHED)] += 1
            self._record(path, system.folder, str(ResolutionMethod.UNMATCHED), matched=False)
            self._emit(ProgressEvent(system.folder, path, None))
            return None

        metadata = parse_metadata(
            game,
            region_chain=self._region_chain,
            language_chain=self._language_chain,
            fallback_name=path.stem,
        )
        if not system.info.arcade:
            # <arcadesystemname> describes arcade hardware. ScreenScraper's
            # `systeme` field is just the platform name, so writing it for a
            # SNES game puts "Super Nintendo" in a tag themes read as arcade
            # board info.
            metadata.arcade_system = None

        assets = parse_media(
            game,
            media_config=self._config.media,
            region_chain=self._region_chain,
        )
        result = ScrapeResult(rom=rom, metadata=metadata, media=assets, method=method)

        self._plan_media(result, system.path)
        if not self._dry_run:
            await self._download_media(result)

        self.stats.matched += 1
        self.stats.by_method[str(method)] += 1
        self._record(path, system.folder, str(method), matched=True)
        self._emit(ProgressEvent(system.folder, path, result))
        return result

    def _plan_media(self, result: ScrapeResult, system_dir: Path) -> None:
        """Assign target paths and account for what this game will cost."""
        images = self._config.images
        planned = self._writer.plan_paths(result, system_dir)
        counted_urls: set[str] = set()
        for asset in result.media:
            target = planned.get(asset.tag)
            if target is None:
                continue
            asset.target = target
            if images.prefer_server_resize:
                asset.url = apply_server_resize(asset.url, images.max_width, images.max_height)
            if not target.exists():
                self.stats.media_planned += 1
                # Only the first tag using a given URL costs bandwidth; the rest
                # are local copies, so counting them would inflate the estimate.
                if asset.url not in counted_urls:
                    counted_urls.add(asset.url)
                    self.stats.bytes_planned += asset.expected_size or 0
                    self.stats.bytes_by_tag[asset.tag] += asset.expected_size or 0

    async def _download_media(self, result: ScrapeResult) -> None:
        wanted = [a for a in result.media if a.target is not None]

        for outcome in await self._downloader.fetch_all(wanted):
            if outcome.written:
                self.stats.media_downloaded += 1
                self.stats.bytes_downloaded += outcome.bytes_written
                await self._shrink(outcome.asset)
            elif outcome.skipped:
                self.stats.media_skipped += 1
            else:
                self.stats.media_failed += 1
                log.debug("media %s failed: %s", outcome.asset.tag, outcome.error)

    async def _shrink(self, asset: MediaAsset) -> None:
        """Local resize pass. A no-op when the server already honoured the cap."""
        images = self._config.images
        if asset.target is None or (not images.max_width and not images.max_height):
            return
        resized = await asyncio.to_thread(
            resize_if_needed,
            asset.target,
            images.max_width,
            images.max_height,
            convert_to=images.convert_to,
        )
        if resized:
            self.stats.media_resized += 1

    def _record(self, path: Path, system: str, method: str, *, matched: bool) -> None:
        # Dry runs must not create resume state -- that would make the next real
        # run skip everything it only pretended to do.
        if self._journal is not None and not self._dry_run:
            self._pending.append((path, system, method, matched))

    def _flush_journal(self) -> None:
        """Commit buffered records. Only ever called once a write has landed.

        If the write raised, this is not reached: the records stay buffered,
        the ROMs stay un-journalled, and the next run redoes them -- cheaply,
        since their media is already on the card.
        """
        if self._journal is None:
            return
        for path, system, method, matched in self._pending:
            self._journal.record(path, system, method, matched=matched)
        self._pending.clear()

    def _fail(self, system: ScannedSystem, path: Path, message: str) -> None:
        self.stats.failed += 1
        log.warning("%s: %s", path.name, message)
        self._emit(ProgressEvent(system.folder, path, None, error=message))

    def _emit(self, event: ProgressEvent) -> None:
        if self._on_progress is not None:
            self._on_progress(event)
