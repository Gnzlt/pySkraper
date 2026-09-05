"""The identification chain in order.

Each step must only be reached when the previous is exhausted, because steps 3
and 4 spend the KO quota and can be confidently wrong in a way a hash cannot.
"""

from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pyskraper.api.client import ApiCredentials, ScreenScraperClient
from pyskraper.api.quota import QuotaGovernor
from pyskraper.config import Config
from pyskraper.core.cache import Cache
from pyskraper.core.identify import Identifier
from pyskraper.core.models import ResolutionMethod, RomFile
from pyskraper.core.naming import clean_name, similarity
from pyskraper.systems import lookup

BASE = "https://api.screenscraper.fr/api2"
CREDS = ApiCredentials(devid="d", devpassword="dp", ssid="u", sspassword="up", softname="pySkraper")
SSUSER = {"id": "u", "maxthreads": "1", "maxrequestspermin": "0", "maxrequestsperday": "10000"}
GAME = {"id": "1234", "noms": [{"region": "wor", "text": "Super Mario World"}]}
NOT_FOUND = httpx.Response(404, text="Rom non trouvée !")


def _ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, text=json.dumps({"header": {}, "response": body}))


def _rom(tmp_path: Path, name: str = "Super Mario World (USA).sfc", system: str = "snes") -> RomFile:
    info = lookup(system)
    assert info is not None
    path = tmp_path / name
    path.write_bytes(b"rom-content-here")
    return RomFile(path=path, system=info, size=path.stat().st_size)


def _config(**identification: Any) -> Config:
    return Config.model_validate({"device": "none", "identification": identification})


async def _identify(rom: RomFile, config: Config | None = None, cache: Cache | None = None) -> Any:
    governor = QuotaGovernor()
    async with ScreenScraperClient(CREDS, governor, retries=0) as client:
        identifier = Identifier(client, config or _config(), cache)
        result = await identifier.identify(rom)
    return result, governor


@respx.mock
async def test_hash_match_stops_the_chain(tmp_path: Path) -> None:
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))
    result, _ = await _identify(_rom(tmp_path))

    assert result.method is ResolutionMethod.HASH
    assert route.call_count == 1, "a content match must not be followed by name lookups"
    assert route.calls[0].request.url.params["md5"]


@respx.mock
async def test_hashing_runs_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hashing must not be done inline in the coroutine.

    A disc image is seconds of solid disk I/O. Doing that on the loop stalls
    every in-flight request and the quota governor's timer with it, so the
    whole run goes as slow as its largest file.
    """
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))

    from pyskraper.core import hasher

    real = hasher.hash_file
    threads: list[str] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(hasher, "hash_file", recording)
    await _identify(_rom(tmp_path))

    assert threads, "the ROM was never hashed"
    assert threading.main_thread().name not in threads, "hashing blocked the event loop"


@respx.mock
async def test_falls_through_hash_then_filename_then_search(tmp_path: Path) -> None:
    info_route = respx.get(f"{BASE}/jeuInfos.php").mock(
        side_effect=[NOT_FOUND, NOT_FOUND, _ok({"jeu": GAME, "ssuser": SSUSER})]
    )
    search_route = respx.get(f"{BASE}/jeuRecherche.php").mock(
        return_value=_ok({"jeux": [{"id": "1234", "noms": [{"text": "Super Mario World"}]}], "ssuser": SSUSER})
    )

    result, _ = await _identify(_rom(tmp_path))

    assert result.method is ResolutionMethod.SEARCH
    assert search_route.call_count == 1
    assert "md5" in info_route.calls[0].request.url.params, "content first"
    assert "md5" not in info_route.calls[1].request.url.params, "then filename"
    assert info_route.calls[2].request.url.params["gameid"] == "1234", "search result re-fetched in full"


@respx.mock
async def test_search_term_is_cleaned(tmp_path: Path) -> None:
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=NOT_FOUND)
    search = respx.get(f"{BASE}/jeuRecherche.php").mock(return_value=_ok({"jeux": [], "ssuser": SSUSER}))

    await _identify(_rom(tmp_path, "Super_Mario_World_(USA)_[!].sfc"))

    assert search.calls[0].request.url.params["recherche"] == "Super Mario World"


@respx.mock
async def test_weak_search_candidates_are_rejected(tmp_path: Path) -> None:
    """A loose title match would put the wrong box art on the card. Below the
    similarity floor we return nothing rather than something plausible."""
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=NOT_FOUND)
    respx.get(f"{BASE}/jeuRecherche.php").mock(
        return_value=_ok({"jeux": [{"id": "99", "noms": [{"text": "Completely Different Game"}]}], "ssuser": SSUSER})
    )

    result, _ = await _identify(_rom(tmp_path))
    assert result.method is ResolutionMethod.UNMATCHED


@respx.mock
async def test_serial_is_tried_for_disc_systems(tmp_path: Path) -> None:
    """.chd and re-ripped discs routinely fail to hash-match, so the serial is
    the step that actually identifies a PS1 library."""
    iso = tmp_path / "Final Fantasy VII.iso"
    iso.write_bytes(b"\x00" * 100 + b"BOOT = cdrom:\\SCUS_941.63;1" + b"\x00" * 100)
    info = lookup("psx")
    assert info is not None
    rom = RomFile(path=iso, system=info, size=iso.stat().st_size)

    route = respx.get(f"{BASE}/jeuInfos.php").mock(side_effect=[NOT_FOUND, _ok({"jeu": GAME, "ssuser": SSUSER})])

    result, _ = await _identify(rom)

    assert result.method is ResolutionMethod.SERIAL
    assert result.serial == "SCUS-94163"
    assert route.calls[1].request.url.params["serialnum"] == "SCUS-94163"


@respx.mock
async def test_serial_is_not_attempted_for_cartridge_systems(tmp_path: Path) -> None:
    route = respx.get(f"{BASE}/jeuInfos.php").mock(side_effect=[NOT_FOUND, _ok({"jeu": GAME, "ssuser": SSUSER})])
    respx.get(f"{BASE}/jeuRecherche.php").mock(return_value=_ok({"jeux": [], "ssuser": SSUSER}))

    result, _ = await _identify(_rom(tmp_path))

    assert result.method is ResolutionMethod.FILENAME
    for call in route.calls:
        assert "serialnum" not in call.request.url.params


@respx.mock
async def test_zipped_console_rom_matches_on_inner_content(tmp_path: Path) -> None:
    archive = tmp_path / "Super Mario World.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Super Mario World.sfc", b"INNER" * 100)
    info = lookup("snes")
    assert info is not None
    rom = RomFile(path=archive, system=info, size=archive.stat().st_size)

    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))

    result, _ = await _identify(rom)

    assert result.method is ResolutionMethod.HASH
    assert result.hash_source == "contents"
    # The hash sent must be of the inner ROM, not of the zip.
    from pyskraper.core.hasher import hash_file

    assert route.calls[0].request.url.params["md5"] != hash_file(archive).md5


class TestCacheInteraction:
    @respx.mock
    async def test_cached_game_costs_no_request(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "c.db")
        rom = _rom(tmp_path)
        route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))

        first, _ = await _identify(rom, cache=cache)
        assert first.method is ResolutionMethod.HASH

        second, _ = await _identify(_rom(tmp_path), cache=cache)
        assert second.method is ResolutionMethod.CACHE
        assert route.call_count == 1, "the second run must not touch the API"

    @respx.mock
    async def test_known_miss_is_not_retried(self, tmp_path: Path) -> None:
        """The point of the misses table: a library of homebrew must not
        re-burn the KO budget on every run."""
        cache = Cache(tmp_path / "c.db")
        respx.get(f"{BASE}/jeuInfos.php").mock(return_value=NOT_FOUND)
        respx.get(f"{BASE}/jeuRecherche.php").mock(return_value=_ok({"jeux": [], "ssuser": SSUSER}))

        _, first_governor = await _identify(_rom(tmp_path), cache=cache)
        assert first_governor.ko_used == 2

        respx.reset()
        route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=NOT_FOUND)
        result, second_governor = await _identify(_rom(tmp_path), cache=cache)

        assert result.method is ResolutionMethod.UNMATCHED
        assert route.call_count == 0, "a known miss must cost nothing"
        assert second_governor.ko_used == 0

    @respx.mock
    async def test_hashes_are_reused_when_the_file_is_unchanged(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "c.db")
        rom = _rom(tmp_path)
        respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))

        await _identify(rom, cache=cache)
        stat = rom.path.stat()
        assert cache.get_hashes(rom.path, stat.st_size, stat.st_mtime) is not None


class TestNameCleaning:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Super Mario World (USA).sfc", "Super Mario World"),
            ("Super_Mario_World.sfc", "Super Mario World"),
            ("Sonic (USA, Europe) [!].md", "Sonic"),
            ("Chrono Trigger (USA) (Rev 1).sfc", "Chrono Trigger"),
            ("Doom (v1.1).wad", "Doom"),
            ("Legend of Zelda, The (USA).nes", "The Legend of Zelda"),
            ("Spider-Man.gba", "Spider-Man"),
        ],
    )
    def test_cleaning(self, raw: str, expected: str) -> None:
        assert clean_name(raw) == expected

    def test_similarity_ranks_sensibly(self) -> None:
        assert similarity("Super Mario World", "Super Mario World") == 1.0
        assert similarity("Super Mario World", "Super Mario Bros") > 0.6
        assert similarity("Super Mario World", "Gran Turismo") < 0.4


class TestArchiveCacheKeying:
    @respx.mock
    async def test_zipped_rom_hits_the_cache_on_a_second_run(self, tmp_path: Path) -> None:
        """Regression: a zipped ROM matches on its *inner* hash but is looked
        up by the *archive's* hash. Caching under the inner one made every
        re-run miss and re-spend a request -- which is the whole point of the
        cache on a 3,000-ROM library."""
        archive = tmp_path / "Super Mario World.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("Super Mario World.sfc", b"INNER" * 100)
        info = lookup("snes")
        assert info is not None

        cache = Cache(tmp_path / "c.db")
        route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))

        def rom() -> RomFile:
            return RomFile(path=archive, system=info, size=archive.stat().st_size)

        first, _ = await _identify(rom(), cache=cache)
        assert first.method is ResolutionMethod.HASH

        second, _ = await _identify(rom(), cache=cache)
        assert second.method is ResolutionMethod.CACHE
        assert route.call_count == 1, "the second run must not re-query a cached zipped ROM"
