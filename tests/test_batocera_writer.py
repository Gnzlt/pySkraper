"""The Batocera/KNULLI writer.

The merge assertions are the ones that matter. A gamelist holds the user's play
history, and a scrape that discards <favorite> or <playcount> is data loss no
amount of re-scraping can undo.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from pyskraper.core.hasher import Hashes
from pyskraper.core.models import GameMetadata, MediaAsset, ResolutionMethod, RomFile, ScrapeResult
from pyskraper.output.batocera import BatoceraWriter
from pyskraper.systems import lookup


def _result(system_dir: Path, stem: str = "Super Mario World", *, with_media: bool = True) -> ScrapeResult:
    info = lookup("snes")
    assert info is not None
    rom_path = system_dir / f"{stem}.sfc"
    rom_path.parent.mkdir(parents=True, exist_ok=True)
    rom_path.write_bytes(b"rom")

    rom = RomFile(
        path=rom_path,
        system=info,
        size=3,
        hashes=Hashes(crc32="B19ED489", md5="0838e531fe22c077528febe14cb3ff7c", sha1="abc", size=3),
    )
    metadata = GameMetadata(
        name="Super Mario World",
        ss_game_id=1234,
        description="Mario must travel through Dinosaur Land.",
        genre="Platform",
        developer="Nintendo EAD",
        publisher="Nintendo",
        release_date="19901121T000000",
        players="2",
        rating=0.92,
    )
    media: list[MediaAsset] = []
    if with_media:
        image = MediaAsset(tag="image", key="ss", url="http://x/ss.png", fmt="png")
        image.downloaded = True
        thumb = MediaAsset(tag="thumbnail", key="box-2D", url="http://x/box.png", fmt="png")
        thumb.downloaded = True
        media = [image, thumb]

    return ScrapeResult(rom=rom, metadata=metadata, media=media, method=ResolutionMethod.HASH)


def test_writes_a_valid_gamelist(tmp_path: Path) -> None:
    writer = BatoceraWriter()
    target = writer.write([_result(tmp_path)], tmp_path)

    root = ET.parse(target).getroot()
    assert root.tag == "gameList"
    game = root.find("game")
    assert game is not None
    assert game.findtext("path") == "./Super Mario World.sfc"
    assert game.findtext("name") == "Super Mario World"
    assert game.findtext("genre") == "Platform"
    assert game.findtext("releasedate") == "19901121T000000"


def test_media_paths_are_relative_with_the_expected_suffixes(tmp_path: Path) -> None:
    writer = BatoceraWriter()
    target = writer.write([_result(tmp_path)], tmp_path)
    game = ET.parse(target).getroot().find("game")
    assert game is not None
    assert game.findtext("image") == "./images/Super Mario World-image.png"
    assert game.findtext("thumbnail") == "./images/Super Mario World-thumb.png"


def test_plan_paths_follows_the_batocera_layout(tmp_path: Path) -> None:
    writer = BatoceraWriter()
    result = _result(tmp_path)
    result.media.append(MediaAsset(tag="video", key="video", url="u", fmt="mp4"))
    result.media.append(MediaAsset(tag="manual", key="manuel", url="u", fmt="pdf"))

    planned = writer.plan_paths(result, tmp_path)
    assert planned["image"] == tmp_path / "images" / "Super Mario World-image.png"
    assert planned["video"] == tmp_path / "videos" / "Super Mario World-video.mp4"
    assert planned["manual"] == tmp_path / "manuals" / "Super Mario World-manual.pdf"


def test_undownloaded_media_is_not_referenced(tmp_path: Path) -> None:
    """Writing a path for a file that failed to download would leave the theme
    pointing at nothing."""
    writer = BatoceraWriter()
    result = _result(tmp_path)
    result.media[0].downloaded = False
    target = writer.write([result], tmp_path)
    game = ET.parse(target).getroot().find("game")
    assert game is not None
    assert game.find("image") is None
    assert game.findtext("thumbnail") is not None


def test_hashes_and_scraper_id_are_written_back(tmp_path: Path) -> None:
    """Re-scrapes short-circuit to a direct gameid lookup, and dedupe/verify
    need this data to work from."""
    writer = BatoceraWriter()
    target = writer.write([_result(tmp_path)], tmp_path)
    game = ET.parse(target).getroot().find("game")
    assert game is not None
    assert game.findtext("crc32") == "B19ED489"
    assert game.findtext("md5") == "0838e531fe22c077528febe14cb3ff7c"
    assert game.findtext("id") == "1234"


def test_device_written_fields_survive_a_rescrape(tmp_path: Path) -> None:
    """<favorite>, <playcount> and friends belong to the handheld. We have no
    business overwriting them."""
    existing = """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./Super Mario World.sfc</path>
    <name>Old Name</name>
    <favorite>true</favorite>
    <playcount>47</playcount>
    <lastplayed>20240102T101500</lastplayed>
    <gametime>3600</gametime>
    <hidden>false</hidden>
    <emulator>libretro</emulator>
    <core>snes9x</core>
  </game>
</gameList>
"""
    (tmp_path / "gamelist.xml").write_text(existing)

    writer = BatoceraWriter(merge=True)
    target = writer.write([_result(tmp_path)], tmp_path)
    game = ET.parse(target).getroot().find("game")
    assert game is not None

    assert game.findtext("name") == "Super Mario World", "scraped metadata should update"
    assert game.findtext("favorite") == "true"
    assert game.findtext("playcount") == "47"
    assert game.findtext("lastplayed") == "20240102T101500"
    assert game.findtext("gametime") == "3600"
    assert game.findtext("emulator") == "libretro"
    assert game.findtext("core") == "snes9x"


def test_games_not_in_this_run_are_left_alone(tmp_path: Path) -> None:
    """A --limit 5 run, or one restricted to a single system, must not wipe the
    rest of the file."""
    existing = """<?xml version="1.0"?>
<gameList>
  <game><path>./Another Game.sfc</path><name>Another Game</name><playcount>3</playcount></game>
</gameList>
"""
    (tmp_path / "gamelist.xml").write_text(existing)

    writer = BatoceraWriter(merge=True)
    target = writer.write([_result(tmp_path)], tmp_path)
    root = ET.parse(target).getroot()

    paths = {g.findtext("path") for g in root.findall("game")}
    assert paths == {"./Another Game.sfc", "./Super Mario World.sfc"}
    other = next(g for g in root.findall("game") if g.findtext("path") == "./Another Game.sfc")
    assert other.findtext("playcount") == "3"


def test_rescraping_does_not_duplicate_entries(tmp_path: Path) -> None:
    writer = BatoceraWriter(merge=True)
    writer.write([_result(tmp_path)], tmp_path)
    target = writer.write([_result(tmp_path)], tmp_path)
    assert len(ET.parse(target).getroot().findall("game")) == 1


def test_corrupt_gamelist_is_preserved_not_destroyed(tmp_path: Path) -> None:
    (tmp_path / "gamelist.xml").write_text("<gameList><game><path>broken")

    writer = BatoceraWriter(merge=True)
    target = writer.write([_result(tmp_path)], tmp_path)

    assert (tmp_path / "gamelist.xml.corrupt").exists(), "the unparseable original must be kept"
    assert ET.parse(target).getroot().find("game") is not None


def test_merge_disabled_starts_fresh(tmp_path: Path) -> None:
    (tmp_path / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList><game><path>./Old.sfc</path></game></gameList>'
    )
    writer = BatoceraWriter(merge=False)
    target = writer.write([_result(tmp_path)], tmp_path)
    paths = {g.findtext("path") for g in ET.parse(target).getroot().findall("game")}
    assert paths == {"./Super Mario World.sfc"}


def test_write_is_atomic(tmp_path: Path) -> None:
    writer = BatoceraWriter()
    writer.write([_result(tmp_path)], tmp_path)
    assert not (tmp_path / "gamelist.xml.part").exists()


# ---- reading back (the hygiene half of the protocol) ----------------------


def test_list_entries_reads_back_what_was_written(tmp_path: Path) -> None:
    writer = BatoceraWriter()
    writer.write([_result(tmp_path)], tmp_path)

    entries = writer.list_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].rom_path == (tmp_path / "Super Mario World.sfc").resolve()
    assert entries[0].name == "Super Mario World"
    assert entries[0].game_id == 1234


def test_list_entries_on_an_unscraped_system_is_empty_not_an_error(tmp_path: Path) -> None:
    assert BatoceraWriter().list_entries(tmp_path) == []


def test_media_index_maps_files_back_to_their_rom(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "Super Mario World-image.png").write_bytes(b"x")
    (images / "Super Mario World-thumb.png").write_bytes(b"x")
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "Super Mario World-video.mp4").write_bytes(b"x")

    index = BatoceraWriter().media_index(tmp_path)
    assert set(index.values()) == {"Super Mario World"}
    assert len(index) == 3


def test_media_index_does_not_confuse_box_with_boxback(tmp_path: Path) -> None:
    """`-box` is a prefix of `-boxback`; a sloppy strip would leave `Gameback`."""
    images = tmp_path / "images"
    images.mkdir()
    (images / "Game-box.png").write_bytes(b"x")
    (images / "Game-boxback.png").write_bytes(b"x")

    assert set(BatoceraWriter().media_index(tmp_path).values()) == {"Game"}


def test_remove_entries_drops_only_the_named_games(tmp_path: Path) -> None:
    writer = BatoceraWriter()
    writer.write([_result(tmp_path, "Keep Me"), _result(tmp_path, "Remove Me")], tmp_path)

    removed = writer.remove_entries([tmp_path / "Remove Me.sfc"], tmp_path)

    assert removed == 1
    assert [entry.rom_path.name for entry in writer.list_entries(tmp_path)] == ["Keep Me.sfc"]


def test_remove_entries_preserves_the_rest_of_the_file(tmp_path: Path) -> None:
    writer = BatoceraWriter()
    writer.write([_result(tmp_path, "Keep Me"), _result(tmp_path, "Remove Me")], tmp_path)
    writer.remove_entries([tmp_path / "Remove Me.sfc"], tmp_path)

    root = ET.parse(writer.gamelist_path(tmp_path)).getroot()
    game = root.find("game")
    assert game is not None
    assert (node := game.find("desc")) is not None and node.text
    assert (node := game.find("developer")) is not None and node.text == "Nintendo EAD"


def test_remove_entries_never_touches_the_rom_or_its_media(tmp_path: Path) -> None:
    writer = BatoceraWriter()
    writer.write([_result(tmp_path, "Remove Me")], tmp_path)
    rom = tmp_path / "Remove Me.sfc"

    writer.remove_entries([rom], tmp_path)

    assert rom.exists()


def test_gamelist_is_byte_identical_regardless_of_scrape_order(tmp_path: Path) -> None:
    """Two runs over the same library must produce the same file.

    Results arrive in scrape-completion order, which is network timing and
    nothing to do with the library. Writing them in that order meant every
    re-scrape looked like a change to anything watching the file.
    """
    first_dir = tmp_path / "a" / "snes"
    second_dir = tmp_path / "b" / "snes"
    names = ["Zelda", "Actraiser", "Super Metroid", "Chrono Trigger"]

    forwards = [_result(first_dir, stem=n) for n in names]
    backwards = [_result(second_dir, stem=n) for n in reversed(names)]

    a = BatoceraWriter().write(forwards, first_dir)
    b = BatoceraWriter().write(backwards, second_dir)

    assert a.read_text() == b.read_text()
