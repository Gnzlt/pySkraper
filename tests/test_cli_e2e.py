"""The `pyskraper` command, end to end.

Each test runs the real CLI (Typer's CliRunner, in-process) against a ROM tree in a
temporary folder that stands in for the SD card, with a config file and an isolated
base directory. Only ScreenScraper is faked: `FakeScreenScraper` answers the API and
the media host through respx, the way the real service does (a 404 "Rom non trouvée"
for a miss, a 401 for a bad login), and records every request. The checks are on what
a user gets: the exit code, the output, gamelist.xml, the media files on the card, the
log file, and the requests that were spent.

No test here touches the network or uses real credentials.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from pyskraper import paths
from pyskraper.cli import app
from pyskraper.exits import EXIT_CONFIG, EXIT_OK, EXIT_PARTIAL, EXIT_QUOTA

runner = CliRunner()

API = "https://api.screenscraper.fr/api2"
SHOT_URL = "https://media.screenscraper.fr/medias/4/ss.png"
BOX_URL = "https://media.screenscraper.fr/medias/4/box.png"

# Distinctive enough that finding one in a log file can only mean a leak.
DEV_PASSWORD = "devpw-4f9c2e"
USER_PASSWORD = "userpw-81d7aa"

MARIO = b"super mario world rom bytes"
ZELDA = b"a link to the past rom bytes"


def ssuser(**overrides: str) -> dict[str, str]:
    return {
        "id": "ana",
        "niveau": "2",
        "maxthreads": "2",
        "maxrequestspermin": "0",
        "maxrequestsperday": "10000",
        "requeststoday": "1",
        "maxrequestskoperday": "200",
        "requestskotoday": "0",
        "maxdownloadspeed": "500",
    } | overrides


def game(game_id: str, title: str) -> dict[str, Any]:
    return {
        "id": game_id,
        "noms": [{"region": "wor", "text": title}],
        "systeme": {"id": "4", "text": "Super Nintendo"},
        "synopsis": [{"langue": "en", "text": f"{title}, the synopsis."}],
        "genres": [{"noms": [{"langue": "en", "text": "Platform"}]}],
        "editeur": {"text": "Nintendo"},
        "developpeur": {"text": "Nintendo EAD"},
        "dates": [{"region": "us", "text": "1991-08-13"}],
        "joueurs": {"text": "2"},
        "note": {"text": "18"},
        "medias": [
            {"type": "ss", "region": "us", "url": SHOT_URL, "format": "png"},
            {"type": "box-2D", "region": "wor", "url": BOX_URL, "format": "png"},
        ],
    }


class FakeScreenScraper:
    """ScreenScraper as respx routes: games are found by MD5, or by file name when the
    lookup carries no hashes, like the real API."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.by_md5: dict[str, dict[str, Any]] = {}
        self.by_name: dict[str, dict[str, Any]] = {}
        self.quota = ssuser()
        self.login_ok = True
        self.fail_after: int | None = None  # raise mid-run after this many lookups
        self.lookups: list[httpx.QueryParams] = []
        self.api = router.route(host="api.screenscraper.fr").mock(side_effect=self._api)
        self.media = router.route(host="media.screenscraper.fr").mock(side_effect=self._media)

    def add(self, content: bytes, file_name: str, entry: dict[str, Any]) -> None:
        self.by_md5[hashlib.md5(content).hexdigest()] = entry
        self.by_name[file_name] = entry

    def _ok(self, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"header": {}, "response": body | {"ssuser": self.quota}}))

    def _api(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if not self.login_ok:
            return httpx.Response(401, text="Erreur de login : vérifiez vos identifiants")
        if endpoint in ("ssuserInfos.php", "systemesListe.php"):
            return self._ok({"systemes": []})
        if endpoint == "jeuRecherche.php":
            return self._ok({"jeux": []})
        assert endpoint == "jeuInfos.php", endpoint
        self.lookups.append(params)
        if self.fail_after is not None and len(self.lookups) > self.fail_after:
            raise RuntimeError("connection lost mid-system")
        found = self.by_md5.get(params["md5"]) if "md5" in params else self.by_name.get(params.get("romnom", ""))
        return self._ok({"jeu": found}) if found else httpx.Response(404, text="Rom non trouvée !")

    def _media(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"PNG:" + request.url.path.encode())

    def requests(self) -> list[httpx.Request]:
        return [call.request for call in self.api.calls]


@pytest.fixture
def card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An SD card's roms/ folder, a config file pointing at it, and an isolated base dir."""
    roms = tmp_path / "card" / "roms"
    (roms / "snes").mkdir(parents=True)
    (roms / "snes" / "Super Mario World (USA).sfc").write_bytes(MARIO)
    (roms / "snes" / "Mystery Homebrew.sfc").write_bytes(b"matches nothing anywhere")
    (roms / "my-homebrew-stuff").mkdir()
    (roms / "my-homebrew-stuff" / "thing.bin").write_bytes(b"not a known system")

    config = tmp_path / "config.yaml"
    config.write_text(
        "screenscraper:\n"
        "  devid: dev\n"
        f"  devpassword: {DEV_PASSWORD}\n"
        "  ssid: ana\n"
        f"  sspassword: {USER_PASSWORD}\n"
        f"paths:\n  roms: {roms}\n  cache: {tmp_path / 'cache'}\n"
        "media:\n  image: [ss]\n  thumbnail: [box-2D]\n"
        "device: none\n"
        "network:\n  retries: 0\n"
    )
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("PYSKRAPER_HOME", str(base))
    monkeypatch.setenv("PYSKRAPER_CONFIG", str(config))
    for name in ("PYSKRAPER_ROMS", "PYSKRAPER_CACHE", "PYSKRAPER_DEVID", "PYSKRAPER_DEVPASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name in ("PYSKRAPER_SSID", "PYSKRAPER_SSPASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield roms
    paths.set_base_dir(None)


@pytest.fixture
def screenscraper() -> Iterator[FakeScreenScraper]:
    with respx.mock(assert_all_called=False) as router:
        fake = FakeScreenScraper(router)
        fake.add(MARIO, "Super Mario World (USA).sfc", game("1234", "Super Mario World"))
        yield fake


def pyskraper(*args: str) -> Any:
    return runner.invoke(app, ["--non-interactive", *args])


def games(roms: Path, system: str = "snes") -> dict[str, ET.Element]:
    root = ET.parse(roms / system / "gamelist.xml").getroot()
    return {str(g.findtext("path")): g for g in root.findall("game")}


def test_scrape_writes_the_card_and_a_rerun_spends_nothing(card: Path, screenscraper: FakeScreenScraper) -> None:
    result = pyskraper("scrape", "-vv")

    assert result.exit_code == EXIT_PARTIAL, result.output  # one ROM matched nothing
    assert "Signed in as ana — 2 thread(s)" in result.output, "the quota grant sets the concurrency"

    # The gamelist holds the matched game, with media paths relative to the system folder.
    assert ET.parse(card / "snes" / "gamelist.xml").getroot().tag == "gameList"
    listed = games(card)
    assert list(listed) == ["./Super Mario World (USA).sfc"]
    mario = listed["./Super Mario World (USA).sfc"]
    assert mario.findtext("name") == "Super Mario World"
    assert mario.findtext("releasedate") == "19910813T000000"
    assert mario.findtext("developer") == "Nintendo EAD"
    assert mario.findtext("genre") == "Platform"
    assert mario.findtext("image") == "./images/Super Mario World (USA)-image.png"
    assert mario.findtext("thumbnail") == "./images/Super Mario World (USA)-thumb.png"
    # Hashes and the ScreenScraper id are written back, for re-scrapes, dedupe and verify.
    assert mario.findtext("md5") == hashlib.md5(MARIO).hexdigest()
    assert mario.findtext("crc32") == f"{zlib.crc32(MARIO):08X}"
    assert mario.findtext("id") == "1234"
    assert mario.find("arcadesystemname") is None, "the platform name is not arcade board info"

    # The media are on the card.
    images = card / "snes" / "images"
    assert (images / "Super Mario World (USA)-image.png").read_bytes() == b"PNG:/medias/4/ss.png"
    assert (images / "Super Mario World (USA)-thumb.png").read_bytes() == b"PNG:/medias/4/box.png"

    # Identification is hash first: one lookup for Mario, carrying all three hashes. The
    # mystery ROM falls back to its name only after its content missed. The unknown
    # system folder costs nothing.
    by_rom = [(p.get("romnom"), "md5" in p) for p in screenscraper.lookups]
    assert by_rom.count(("Super Mario World (USA).sfc", True)) == 1
    assert ("Super Mario World (USA).sfc", False) not in by_rom
    assert by_rom.index(("Mystery Homebrew.sfc", True)) < by_rom.index(("Mystery Homebrew.sfc", False))
    assert all(p.get("romnom") != "thing.bin" for p in screenscraper.lookups)
    mario_lookup = next(p for p in screenscraper.lookups if p.get("romnom") == "Super Mario World (USA).sfc")
    assert mario_lookup["sha1"] == hashlib.sha1(MARIO).hexdigest()
    assert mario_lookup["crc"].upper() == f"{zlib.crc32(MARIO):08X}"
    sent = (mario_lookup["systemeid"], mario_lookup["romtype"], mario_lookup["romtaille"])
    assert sent == ("4", "rom", str(len(MARIO)))
    assert "serialnum" not in mario_lookup and "gameid" not in mario_lookup, "empty parameters are left out"
    searched = [r.url.params["recherche"] for r in screenscraper.requests() if r.url.path.endswith("jeuRecherche.php")]
    assert searched == ["Mystery Homebrew"], "a search is the last resort, with a cleaned term"

    # Every API request is authenticated and asks for JSON.
    for request in screenscraper.requests():
        params = request.url.params
        assert (params["devid"], params["softname"], params["output"]) == ("dev", "pySkraper", "json")
        assert (params["devpassword"], params["ssid"], params["sspassword"]) == (DEV_PASSWORD, "ana", USER_PASSWORD)

    # The full log is written whatever the verbosity, and never holds a credential.
    log = (Path("base") / "data" / "pyskraper.log").read_text()
    assert "gamelist.xml (1 games)" in log
    assert DEV_PASSWORD not in log and USER_PASSWORD not in log

    # A second run resumes: the matched game is journalled and the miss is cached, so it
    # spends no lookups at all, and the gamelist keeps one entry.
    spent = len(screenscraper.lookups)
    again = pyskraper("scrape")
    assert "Resuming" in again.output
    assert len(screenscraper.lookups) == spent, "a rerun must not re-spend requests or the KO quota"
    assert list(games(card)) == ["./Super Mario World (USA).sfc"]

    # --restart redoes the work but still never duplicates an entry.
    pyskraper("scrape", "--restart")
    assert list(games(card)) == ["./Super Mario World (USA).sfc"]


def test_dry_run_estimates_the_cost_and_writes_nothing(card: Path, screenscraper: FakeScreenScraper) -> None:
    result = pyskraper("scrape", "--dry-run")

    assert "Cost estimate (dry run" in result.output, result.output
    assert not (card / "snes" / "gamelist.xml").exists()
    assert not (card / "snes" / "images").exists()
    assert screenscraper.media.call_count == 0, "a dry run must not spend download bandwidth"

    # And it left no resume state behind: the real run that follows does the work.
    real = pyskraper("scrape")
    assert "Resuming" not in real.output
    assert list(games(card)) == ["./Super Mario World (USA).sfc"]


def test_quota_threshold_stops_cleanly_and_keeps_what_it_resolved(card: Path, screenscraper: FakeScreenScraper) -> None:
    screenscraper.quota = ssuser(maxrequestsperday="100", requeststoday="99")

    result = pyskraper("scrape")

    assert result.exit_code == EXIT_QUOTA, result.output
    assert "Stopped early: quota threshold reached." in result.output


def test_a_bad_login_fails_before_any_lookup(card: Path, screenscraper: FakeScreenScraper) -> None:
    screenscraper.login_ok = False

    result = pyskraper("scrape")

    assert result.exit_code == EXIT_CONFIG
    assert "AuthError" in result.output
    assert screenscraper.lookups == []
    assert len(screenscraper.requests()) == 1, "a bad password cannot be fixed by asking again"


def test_limit_caps_the_work_per_system(card: Path, screenscraper: FakeScreenScraper) -> None:
    for name in ("A.sfc", "B.sfc", "C.sfc"):
        (card / "snes" / name).write_bytes(name.encode())

    pyskraper("scrape", "--limit", "2")

    assert len({p.get("romnom") for p in screenscraper.lookups}) == 2


def test_name_fallbacks_can_be_turned_off_and_hashing_skipped(card: Path, screenscraper: FakeScreenScraper) -> None:
    config = Path("config.yaml")
    config.write_text(config.read_text() + "identification:\n  filename_fallback: false\n  search_fallback: false\n")

    pyskraper("scrape", "--system", "snes")
    assert [p.get("romnom") for p in screenscraper.lookups].count("Mystery Homebrew.sfc") == 1

    screenscraper.lookups.clear()
    config.write_text(config.read_text().replace("filename_fallback: false", "filename_fallback: true"))
    pyskraper("scrape", "--restart", "--no-cache", "--no-hash")
    assert screenscraper.lookups and not any("md5" in p for p in screenscraper.lookups)
    assert "Super Mario World (USA).sfc" in [p.get("romnom") for p in screenscraper.lookups]


def test_an_interrupted_system_keeps_what_it_resolved(card: Path, screenscraper: FakeScreenScraper) -> None:
    """The regression that cost a real card 510 games: a fatal error part-way through a
    system used to drop every game it had resolved, while the journal said they were done."""
    (card / "snes" / "Mystery Homebrew.sfc").unlink()
    for index, name in enumerate(("Zelda.sfc", "Kirby.sfc", "Yoshi.sfc")):
        content = ZELDA + name.encode()
        (card / "snes" / name).write_bytes(content)
        screenscraper.add(content, name, game(str(2000 + index), name.removesuffix(".sfc")))
    screenscraper.fail_after = 2

    crashed = pyskraper("scrape")
    assert isinstance(crashed.exception, RuntimeError)
    resolved = set(games(card))
    assert len(resolved) == 2, "an interrupted system must still leave a gamelist"

    # Nothing is journalled as done unless it is in the gamelist: the next run picks up
    # exactly the two games that were lost, and spends no lookup on the two it kept.
    screenscraper.fail_after = None
    screenscraper.lookups.clear()
    pyskraper("scrape")
    assert len(games(card)) == 4
    assert {f"./{p.get('romnom')}" for p in screenscraper.lookups}.isdisjoint(resolved)


def test_doctor_quota_systems_hash_and_devices(card: Path, screenscraper: FakeScreenScraper) -> None:
    doctor = pyskraper("doctor")
    assert doctor.exit_code == EXIT_OK, doctor.output
    assert "Developer and member credentials present" in doctor.output
    assert "1 mapped system(s), 2 ROMs" in doctor.output
    assert "1 unmapped folder(s): my-homebrew-stuff" in doctor.output
    assert "API credentials verified" in doctor.output

    quota = pyskraper("quota")
    assert quota.exit_code == EXIT_OK
    assert "1 / 10000" in quota.output and "0 / 200" in quota.output

    systems = pyskraper("systems")
    assert "snes" in systems.output and "unmapped" in systems.output

    rom = card / "snes" / "Super Mario World (USA).sfc"
    hashed = pyskraper("hash", str(rom))
    assert hashlib.md5(MARIO).hexdigest().upper() in hashed.output.upper()
    assert "systemeid = 4" in hashed.output

    devices = pyskraper("devices")
    assert devices.exit_code == EXIT_OK and "Device profiles" in devices.output

    screenscraper.login_ok = False
    assert pyskraper("doctor").exit_code == EXIT_CONFIG
