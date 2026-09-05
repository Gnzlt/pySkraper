"""The identification chain.

Strict ordering, each step reached only when the previous one is exhausted::

    cache -> hashes (crc32+md5+sha1) -> serialnum -> romnom+romtaille -> jeuRecherche.php

The ordering is the entire point of this tool. Content identifies a ROM; a
filename merely describes it, and describes it in whatever convention the person
who dumped it happened to use. Two further facts make the tail of the chain
genuinely a last resort rather than a stylistic preference:

* steps 3 and 4 spend the **KO quota** on a miss -- the small budget -- and
* a filename match can be confidently *wrong*, where a hash match cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..api.client import ScreenScraperClient
from ..api.errors import NotFoundError
from ..config import Config
from .archives import candidate_hashes
from .cache import Cache
from .hasher import Hashes, hash_file_async
from .models import ResolutionMethod, RomFile
from .naming import clean_name, similarity
from .serials import extract_serial

__all__ = ["Identification", "Identifier"]

log = logging.getLogger(__name__)

# Below this title similarity a search result is more likely a different game
# than a loose match, and scraping it would put the wrong box art on the card.
MIN_SEARCH_SIMILARITY = 0.55


@dataclass
class Identification:
    game: dict[str, Any] | None
    method: ResolutionMethod
    hashes: Hashes | None = None
    serial: str | None = None
    hash_source: str | None = None

    @property
    def matched(self) -> bool:
        return self.game is not None


class Identifier:
    """Resolves one ROM to a ScreenScraper game, spending as little quota as possible."""

    def __init__(self, client: ScreenScraperClient, config: Config, cache: Cache | None = None) -> None:
        self._client = client
        self._config = config
        self._cache = cache

    async def identify(self, rom: RomFile) -> Identification:
        ident = self._config.identification
        systeme_id = rom.system.systeme_id

        primary = await self._hashes_for(rom)
        rom.hashes = primary

        if primary is not None and self._cache is not None:
            cached = self._cache.get_game(primary.md5, systeme_id)
            if cached is not None:
                return Identification(cached, ResolutionMethod.CACHE, hashes=primary)

            if self._cache.is_miss(primary.md5, systeme_id):
                # Already failed recently. Asking again would spend KO quota to
                # learn what we already know.
                log.debug("%s: known miss, skipping", rom.name)
                return Identification(None, ResolutionMethod.UNMATCHED, hashes=primary)

        # --- 1. content ---------------------------------------------------
        for source, hashes in self._hash_candidates(rom, primary) if ident.use_hash else []:
            lookup = hashes.as_lookup()
            if not lookup:
                continue
            game = await self._lookup(
                systeme_id=systeme_id,
                rom_name=rom.name,
                rom_size=hashes.size,
                crc=lookup["crc"],
                md5=lookup["md5"],
                sha1=lookup["sha1"],
            )
            if game:
                rom.hashes = hashes
                # Cache under the *file's* hash, not the matched one. For a
                # zipped ROM those differ: we match on the inner ROM but we look
                # the cache up by the archive, because that is what is on disk.
                # Storing the inner hash made every re-run miss the cache and
                # re-spend a request.
                self._remember(primary or hashes, systeme_id, game)
                return Identification(game, ResolutionMethod.HASH, hashes=hashes, hash_source=source)

        # --- 2. disc serial ------------------------------------------------
        serial: str | None = None
        if ident.use_serial and rom.system.disc:
            serial = extract_serial(rom.path)
            if serial:
                game = await self._lookup(systeme_id=systeme_id, rom_name=rom.name, serial=serial)
                if game:
                    self._remember(primary, systeme_id, game)
                    return Identification(game, ResolutionMethod.SERIAL, hashes=primary, serial=serial)

        # --- 3. filename ---------------------------------------------------
        if ident.filename_fallback:
            game = await self._lookup(systeme_id=systeme_id, rom_name=rom.name, rom_size=rom.size)
            if game:
                self._remember(primary, systeme_id, game)
                return Identification(game, ResolutionMethod.FILENAME, hashes=primary, serial=serial)

        # --- 4. search -----------------------------------------------------
        if ident.search_fallback:
            game = await self._search(rom)
            if game:
                self._remember(primary, systeme_id, game)
                return Identification(game, ResolutionMethod.SEARCH, hashes=primary, serial=serial)

        self._remember_miss(primary, systeme_id, rom)
        return Identification(None, ResolutionMethod.UNMATCHED, hashes=primary, serial=serial)

    # ---- helpers ---------------------------------------------------------

    async def _hashes_for(self, rom: RomFile) -> Hashes | None:
        """The primary hashes for cache keying, read from cache when unchanged."""
        max_size = self._config.identification.max_hash_size
        try:
            stat = rom.path.stat()
        except OSError:
            return None

        if self._cache is not None:
            cached = self._cache.get_hashes(rom.path, stat.st_size, stat.st_mtime)
            if cached is not None:
                return cached

        try:
            # Off the event loop. Hashing a Dreamcast image takes seconds of
            # solid disk I/O, and doing that inline stalls every in-flight
            # request and the quota governor's timer along with it.
            hashes = await hash_file_async(rom.path, max_size=max_size)
        except OSError as exc:
            log.warning("Could not hash %s: %s", rom.name, exc)
            return None

        if self._cache is not None:
            self._cache.put_hashes(rom.path, stat.st_size, stat.st_mtime, hashes)
        return hashes

    def _hash_candidates(self, rom: RomFile, file_hashes: Hashes | None) -> list[tuple[str, Hashes]]:
        try:
            return list(
                candidate_hashes(
                    rom.path,
                    rom.system,
                    policy=self._config.identification.hash_archives,
                    max_size=self._config.identification.max_hash_size,
                    file_hashes=file_hashes,
                )
            )
        except OSError as exc:
            log.warning("Could not hash %s: %s", rom.name, exc)
            return []

    async def _lookup(self, **kwargs: Any) -> dict[str, Any] | None:
        try:
            game = await self._client.game_info(**kwargs)
        except NotFoundError:
            self._client.governor.note_ko()
            return None
        return game or None

    async def _search(self, rom: RomFile) -> dict[str, Any] | None:
        term = clean_name(rom.name)
        if not term:
            return None

        try:
            candidates = await self._client.search(systeme_id=rom.system.systeme_id, term=term)
        except NotFoundError:
            self._client.governor.note_ko()
            return None

        best: tuple[float, dict[str, Any]] | None = None
        for candidate in candidates:
            names = candidate.get("noms")
            titles = [str(n.get("text", "")) for n in names if isinstance(n, dict)] if isinstance(names, list) else []
            for title in titles:
                score = similarity(term, title)
                if best is None or score > best[0]:
                    best = (score, candidate)

        if best is None or best[0] < MIN_SEARCH_SIMILARITY:
            log.debug("%s: no search candidate above the similarity floor", rom.name)
            return None

        # Search results are abbreviated; re-fetch by id so media is complete.
        game_id = best[1].get("id")
        if game_id is None:
            return None
        try:
            return await self._client.game_info(game_id=int(str(game_id))) or None
        except (NotFoundError, ValueError):
            return None

    def _remember(self, hashes: Hashes | None, systeme_id: int, game: dict[str, Any]) -> None:
        if self._cache is None or hashes is None or hashes.truncated:
            return
        self._cache.put_game(hashes.md5, systeme_id, game, hashes=hashes)
        self._cache.forget_miss(hashes.md5, systeme_id)

    def _remember_miss(self, hashes: Hashes | None, systeme_id: int, rom: RomFile) -> None:
        if self._cache is None or hashes is None or hashes.truncated:
            return
        self._cache.put_miss(hashes.md5, systeme_id, rom_name=rom.name, reason="no match")
