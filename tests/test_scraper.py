"""End-to-end orchestration against a mocked API.

This is the M1 exit criterion in test form: ROMs on disk go in, a valid
gamelist and downloaded media come out, and the identification order is
hash-first with filenames as a genuine last resort.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pyskraper.api.client import ApiCredentials, ScreenScraperClient
from pyskraper.api.quota import QuotaGovernor
from pyskraper.config import Config
from pyskraper.core.models import ResolutionMethod
from pyskraper.core.scanner import scan_tree
from pyskraper.core.scraper import Scraper
from pyskraper.output.batocera import BatoceraWriter

BASE = "https://api.screenscraper.fr/api2"
IMAGE_URL = "https://media.screenscraper.fr/ss.png"
BOX_URL = "https://media.screenscraper.fr/box.png"

CREDS = ApiCredentials(devid="d", devpassword="dp", ssid="u", sspassword="up", softname="pySkraper")

SSUSER = {
    "id": "u",
    "maxthreads": "2",
    "maxrequestspermin": "0",
    "maxrequestsperday": "10000",
    "requeststoday": "1",
    "maxrequestskoperday": "200",
    "requestskotoday": "0",
}

GAME = {
    "id": "1234",
    "noms": [{"region": "wor", "text": "Super Mario World"}],
    "synopsis": [{"langue": "en", "text": "Mario travels through Dinosaur Land."}],
    "genres": [{"noms": [{"langue": "en", "text": "Platform"}]}],
    "editeur": {"text": "Nintendo"},
    "developpeur": {"text": "Nintendo EAD"},
    "dates": [{"region": "us", "text": "1991-08-13"}],
    "joueurs": {"text": "2"},
    "note": {"text": "18"},
    "medias": [
        {"type": "ss", "region": "us", "url": IMAGE_URL, "format": "png"},
        {"type": "box-2D", "region": "wor", "url": BOX_URL, "format": "png"},
    ],
}


def _ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, text=json.dumps({"header": {}, "response": body}))


@pytest.fixture
def library(tmp_path: Path) -> Path:
    snes = tmp_path / "snes"
    snes.mkdir()
    (snes / "Super Mario World.sfc").write_bytes(b"rom-content")
    return tmp_path


def _config(roms: Path, **overrides: Any) -> Config:
    data: dict[str, Any] = {
        "paths": {"roms": str(roms)},
        "media": {"image": ["ss"], "thumbnail": ["box-2D"]},
        "device": "none",
    }
    data.update(overrides)
    return Config.model_validate(data)


async def _run(config: Config, roms: Path, **kwargs: Any) -> Any:
    governor = QuotaGovernor()
    async with ScreenScraperClient(CREDS, governor, retries=0) as client:
        await client.user_info()
        scraper = Scraper(config, client, BatoceraWriter(), **kwargs)
        return await scraper.run(scan_tree(roms))


@respx.mock
async def test_full_run_writes_gamelist_and_media(library: Path) -> None:
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"SSPNG"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"BOXPNG"))

    stats = await _run(_config(library), library)

    assert stats.matched == 1
    assert stats.unmatched == 0
    assert stats.media_downloaded == 2
    assert stats.exit_code == 0

    snes = library / "snes"
    assert (snes / "images" / "Super Mario World-image.png").read_bytes() == b"SSPNG"
    assert (snes / "images" / "Super Mario World-thumb.png").read_bytes() == b"BOXPNG"

    game = ET.parse(snes / "gamelist.xml").getroot().find("game")
    assert game is not None
    assert game.findtext("name") == "Super Mario World"
    assert game.findtext("releasedate") == "19910813T000000"
    assert game.findtext("image") == "./images/Super Mario World-image.png"
    assert game.findtext("crc32") is not None


@respx.mock
async def test_identification_is_hash_first(library: Path) -> None:
    """The defining behaviour of this tool: content identifies a ROM, and the
    very first lookup carries all three hashes."""
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"x"))

    stats = await _run(_config(library), library)

    params = route.calls[0].request.url.params
    assert params["md5"] and params["sha1"] and params["crc"]
    assert stats.by_method[str(ResolutionMethod.HASH)] == 1
    assert route.call_count == 1, "a hash hit must not be followed by a filename lookup"


@respx.mock
async def test_filename_fallback_only_after_hashes_miss(library: Path) -> None:
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    route = respx.get(f"{BASE}/jeuInfos.php").mock(
        side_effect=[
            httpx.Response(404, text="Rom non trouvée !"),
            _ok({"jeu": GAME, "ssuser": SSUSER}),
        ]
    )
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"x"))

    stats = await _run(_config(library), library)

    assert route.call_count == 2
    assert "md5" in route.calls[0].request.url.params, "first attempt is by content"
    assert "md5" not in route.calls[1].request.url.params, "fallback is by name only"
    assert stats.by_method[str(ResolutionMethod.FILENAME)] == 1


@respx.mock
async def test_unmatched_rom_is_recorded_and_charged_to_ko_quota(library: Path) -> None:
    """A miss is not a silent no-op: it costs the scarce KO budget, so it has
    to be counted."""
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=httpx.Response(404, text="Rom non trouvée !"))
    respx.get(f"{BASE}/jeuRecherche.php").mock(return_value=_ok({"jeux": [], "ssuser": SSUSER}))

    governor = QuotaGovernor()
    async with ScreenScraperClient(CREDS, governor, retries=0) as client:
        await client.user_info()
        scraper = Scraper(_config(library), client, BatoceraWriter())
        stats = await scraper.run(scan_tree(library))

    assert stats.unmatched == 1
    assert stats.matched == 0
    assert governor.ko_used == 2, "the hash lookup and the filename lookup both missed"
    assert stats.exit_code == 1
    assert not (library / "snes" / "gamelist.xml").exists()


@respx.mock
async def test_name_based_fallbacks_can_be_disabled(library: Path) -> None:
    """--no-hash exists for people who want the old behaviour; its inverse
    exists for people who want content matching only."""
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=httpx.Response(404, text="non trouvée"))

    config = _config(library, identification={"filename_fallback": False, "search_fallback": False})
    stats = await _run(config, library)

    assert route.call_count == 1, "only the content lookup should have been attempted"
    assert stats.unmatched == 1


@respx.mock
async def test_dry_run_writes_nothing(library: Path) -> None:
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))
    image = respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))

    stats = await _run(_config(library), library, dry_run=True)

    assert stats.matched == 1
    assert image.call_count == 0, "a dry run must not spend download bandwidth"
    assert not (library / "snes" / "gamelist.xml").exists()
    assert not (library / "snes" / "images").exists()


@respx.mock
async def test_quota_exhaustion_stops_cleanly(library: Path) -> None:
    """Hitting the quota is a stop, not a crash: exit code 3, and whatever was
    already resolved still gets written."""
    (library / "snes" / "Zelda.smc").write_bytes(b"another")
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    respx.get(f"{BASE}/jeuInfos.php").mock(
        return_value=_ok({"jeu": GAME, "ssuser": dict(SSUSER, maxrequestsperday="100", requeststoday="99")})
    )
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"x"))

    stats = await _run(_config(library), library)

    assert stats.stopped_early
    assert stats.exit_code == 3


@respx.mock
async def test_limit_caps_work_per_system(library: Path) -> None:
    for name in ("A.sfc", "B.sfc", "C.sfc"):
        (library / "snes" / name).write_bytes(b"x" + name.encode())
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"x"))

    stats = await _run(_config(library), library, limit=2)

    assert stats.scanned == 2
    assert route.call_count == 2


@respx.mock
async def test_unknown_system_folder_costs_no_requests(library: Path) -> None:
    weird = library / "my-homebrew-stuff"
    weird.mkdir()
    (weird / "thing.bin").write_bytes(b"x")
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"x"))

    await _run(_config(library), library)

    assert route.call_count == 1, "only the snes ROM should have been looked up"


@respx.mock
async def test_arcadesystemname_only_written_for_arcade_systems(library: Path) -> None:
    """ScreenScraper's `systeme` field is just the platform name. Writing it as
    <arcadesystemname> would put "Super Nintendo" in a tag themes read as
    arcade board information."""
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    respx.get(f"{BASE}/jeuInfos.php").mock(
        return_value=_ok({"jeu": dict(GAME, systeme={"id": "4", "text": "Super Nintendo"}), "ssuser": SSUSER})
    )
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"x"))

    await _run(_config(library), library)

    game = ET.parse(library / "snes" / "gamelist.xml").getroot().find("game")
    assert game is not None
    assert game.find("arcadesystemname") is None


@respx.mock
async def test_resume_skips_completed_roms(library: Path, tmp_path: Path) -> None:
    """The point of the journal: an interrupted run must not re-spend requests
    on work it already finished."""
    from pyskraper.core.journal import RunJournal

    for name in ("A.sfc", "B.sfc"):
        (library / "snes" / name).write_bytes(b"x" + name.encode())

    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"x"))

    journal = RunJournal(tmp_path / "run.jsonl")
    journal.load()
    with journal:
        governor = QuotaGovernor()
        async with ScreenScraperClient(CREDS, governor, retries=0) as client:
            await client.user_info()
            first = await Scraper(_config(library), client, BatoceraWriter(), journal=journal).run(scan_tree(library))
    assert first.matched == 3
    calls_after_first = route.call_count

    reopened = RunJournal(tmp_path / "run.jsonl")
    reopened.load()
    with reopened:
        governor = QuotaGovernor()
        async with ScreenScraperClient(CREDS, governor, retries=0) as client:
            await client.user_info()
            second = await Scraper(_config(library), client, BatoceraWriter(), journal=reopened).run(scan_tree(library))

    assert second.resumed == 3
    assert second.scanned == 0
    assert route.call_count == calls_after_first, "resumed ROMs must cost no requests"


@respx.mock
async def test_dry_run_does_not_create_resume_state(library: Path, tmp_path: Path) -> None:
    """Otherwise the next real run would skip everything it only pretended to do."""
    from pyskraper.core.journal import RunJournal

    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": GAME, "ssuser": SSUSER}))

    journal = RunJournal(tmp_path / "run.jsonl")
    journal.load()
    with journal:
        governor = QuotaGovernor()
        async with ScreenScraperClient(CREDS, governor, retries=0) as client:
            await client.user_info()
            await Scraper(_config(library), client, BatoceraWriter(), dry_run=True, journal=journal).run(
                scan_tree(library)
            )

    assert RunJournal(tmp_path / "run.jsonl").load() == set()


@respx.mock
async def test_interrupted_system_still_writes_what_it_resolved(library: Path, tmp_path: Path) -> None:
    """The regression that cost a real card 510 games.

    Media is downloaded and the journal is written per ROM, but the gamelist
    used to be written only after a whole system finished.  A Ctrl-C or a fatal
    API error part-way through discarded every resolved game in that system --
    while the journal still recorded them as done, so no later run went back
    for them.  Their artwork sat on the card with nothing referencing it.
    """
    from pyskraper.core.journal import RunJournal

    for name in ("A.sfc", "B.sfc", "C.sfc"):
        (library / "snes" / name).write_bytes(b"x" + name.encode())

    seen = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        # Resolve two games, then die the way a dropped connection does.
        nonlocal seen
        seen += 1
        if seen > 2:
            raise RuntimeError("connection lost mid-system")
        return _ok({"jeu": GAME, "ssuser": SSUSER})

    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": SSUSER}))
    respx.get(f"{BASE}/jeuInfos.php").mock(side_effect=flaky)
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"x"))
    respx.get(BOX_URL).mock(return_value=httpx.Response(200, content=b"x"))

    journal = RunJournal(tmp_path / "run.jsonl")
    journal.load()
    with journal, pytest.raises(RuntimeError):
        governor = QuotaGovernor()
        async with ScreenScraperClient(CREDS, governor, retries=0) as client:
            await client.user_info()
            scraper = Scraper(_config(library), client, BatoceraWriter(), journal=journal)
            await scraper.run(scan_tree(library))

    gamelist = library / "snes" / "gamelist.xml"
    assert gamelist.exists(), "an interrupted system must still leave a gamelist"
    listed = {g.findtext("path") for g in ET.parse(gamelist).getroot().findall("game")}
    assert len(listed) == 2

    # The invariant: nothing is journalled as done unless it is in the gamelist,
    # so whatever was lost gets retried instead of being skipped forever.
    done = RunJournal(tmp_path / "run.jsonl").load()
    assert {Path(p).name for p in done} == {p.lstrip("./") for p in listed if p}
