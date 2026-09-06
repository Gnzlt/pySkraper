"""Library verification: drift, orphans and dead metadata entries.

`verify` is the read-only half of library hygiene, and `--clean-orphans` is
deliberately the only thing it can remove -- media and metadata entries, both
of which a re-scrape regenerates. It must never touch a ROM.
"""

from __future__ import annotations

from pathlib import Path

from pyskraper.core.cache import Cache
from pyskraper.core.scanner import scan_tree
from pyskraper.core.verify import VerifyReport, clean_orphans, verify_library
from pyskraper.output.batocera import BatoceraWriter

GAMELIST = (
    '<?xml version="1.0"?>\n<gameList>\n'
    "  <game><path>./Game.sfc</path><name>Game</name><id>7</id></game>\n"
    "</gameList>\n"
)


def _library(tmp_path: Path) -> tuple[Path, Path]:
    roms = tmp_path / "roms"
    snes = roms / "snes"
    (snes / "images").mkdir(parents=True)
    (snes / "Game.sfc").write_bytes(b"original content")
    (snes / "images" / "Game-image.png").write_bytes(b"art")
    (snes / "gamelist.xml").write_text(GAMELIST)
    return roms, snes


def _verify(roms: Path, cache_path: Path, *, rehash: bool = True) -> VerifyReport:
    with Cache(cache_path) as cache:
        return verify_library(scan_tree(roms), cache=cache, writer=BatoceraWriter(), rehash=rehash)


def test_a_consistent_library_is_clean(tmp_path: Path) -> None:
    roms, _snes = _library(tmp_path)
    _verify(roms, tmp_path / "c.db")  # first pass records the hashes
    report = _verify(roms, tmp_path / "c.db")

    assert report.clean
    assert report.drifted == 0
    assert report.orphan_media == 0
    assert report.missing_roms == 0


def test_detects_content_drift(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    _verify(roms, tmp_path / "c.db")

    (snes / "Game.sfc").write_bytes(b"different content entirely")
    report = _verify(roms, tmp_path / "c.db")

    assert report.drifted == 1
    path, before, after = report.systems[0].drifted[0]
    assert path.name == "Game.sfc"
    assert before != after


def test_a_new_rom_is_not_drift(tmp_path: Path) -> None:
    """Never hashed before means nothing to compare against, not a change."""
    roms, snes = _library(tmp_path)
    _verify(roms, tmp_path / "c.db")

    (snes / "Newcomer.sfc").write_bytes(b"new")
    report = _verify(roms, tmp_path / "c.db")

    assert report.drifted == 0
    assert [path.name for path in report.systems[0].unlisted_roms] == ["Newcomer.sfc"]


def test_detects_a_metadata_entry_whose_rom_is_gone(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    (snes / "Other.sfc").write_bytes(b"other")  # keep the system non-empty
    (snes / "Game.sfc").unlink()
    report = _verify(roms, tmp_path / "c.db")

    assert [path.name for path in report.systems[0].missing_roms] == ["Game.sfc"]


def test_detects_orphan_media(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    (snes / "images" / "Ghost-image.png").write_bytes(b"art for nothing")
    report = _verify(roms, tmp_path / "c.db")

    assert [path.name for path in report.systems[0].orphan_media] == ["Ghost-image.png"]


def test_detects_a_rom_that_was_never_scraped(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    (snes / "Unscraped.sfc").write_bytes(b"x")
    report = _verify(roms, tmp_path / "c.db")

    assert [path.name for path in report.systems[0].unlisted_roms] == ["Unscraped.sfc"]


def test_clean_orphans_without_apply_changes_nothing(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    orphan = snes / "images" / "Ghost-image.png"
    orphan.write_bytes(b"art for nothing")
    report = _verify(roms, tmp_path / "c.db")

    media, entries, errors = clean_orphans(report, apply=False, writer=BatoceraWriter())

    assert (media, entries, errors) == (1, 0, [])
    assert orphan.exists()


def test_clean_orphans_removes_media_and_entries_but_never_roms(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    (snes / "images" / "Ghost-image.png").write_bytes(b"art for nothing")
    (snes / "Other.sfc").write_bytes(b"other")
    (snes / "Game.sfc").unlink()  # the gamelist still lists it

    report = _verify(roms, tmp_path / "c.db")
    media, entries, errors = clean_orphans(report, apply=True, writer=BatoceraWriter())

    assert errors == []
    assert media == 2  # Ghost-image.png, and Game-image.png whose ROM is gone
    assert entries == 1
    assert not (snes / "images" / "Ghost-image.png").exists()
    assert BatoceraWriter().list_entries(snes) == []
    # The one thing that must survive all of this:
    assert (snes / "Other.sfc").read_bytes() == b"other"


def test_detects_a_legacy_scraper_folder(tmp_path: Path) -> None:
    """A folder of art no writer put there -- leftovers from a different tool."""
    roms, snes = _library(tmp_path)
    (snes / "imgs").mkdir()
    (snes / "imgs" / "Game.png").write_bytes(b"art")
    (snes / "manual").mkdir()
    (snes / "manual" / "Game.pdf").write_bytes(b"manual")
    report = _verify(roms, tmp_path / "c.db")

    folders = {folder.name: len(files) for folder, files in report.systems[0].legacy_media}
    assert folders == {"imgs": 1, "manual": 1}
    assert report.legacy_media == 2
    assert not report.clean


def test_a_mixed_folder_is_never_treated_as_legacy_media(tmp_path: Path) -> None:
    """One file we don't recognise, and the whole folder is left alone."""
    roms, snes = _library(tmp_path)
    (snes / "imgs").mkdir()
    (snes / "imgs" / "Game.png").write_bytes(b"art")
    (snes / "imgs" / "readme.txt").write_bytes(b"notes")
    report = _verify(roms, tmp_path / "c.db")

    assert report.systems[0].legacy_media == []


def test_saves_and_similar_emulator_folders_are_never_touched(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    (snes / "saves").mkdir()
    (snes / "saves" / "Game.srm.png").write_bytes(b"not actually art")
    report = _verify(roms, tmp_path / "c.db")

    assert report.systems[0].legacy_media == []


def test_a_folder_the_gamelist_still_points_at_is_not_legacy_media(tmp_path: Path) -> None:
    """The case that would have deleted art the device is displaying.

    A game this tool never matched keeps whatever `<image>` the old scraper
    wrote. Extension-wise the folder looks exactly like leftovers -- the only
    thing saying otherwise is the reference.
    """
    roms, snes = _library(tmp_path)
    (snes / "imgs").mkdir()
    (snes / "imgs" / "Old.png").write_bytes(b"art in use")
    (snes / "Old.sfc").write_bytes(b"never matched")
    (snes / "gamelist.xml").write_text(
        '<?xml version="1.0"?>\n<gameList>\n'
        "  <game><path>./Game.sfc</path><name>Game</name><id>7</id></game>\n"
        "  <game><path>./Old.sfc</path><name>Old</name><image>./imgs/Old.png</image></game>\n"
        "</gameList>\n"
    )
    report = _verify(roms, tmp_path / "c.db")

    assert report.systems[0].legacy_media == []
    assert (snes / "imgs" / "Old.png").exists()


def test_one_referenced_file_protects_the_whole_folder(tmp_path: Path) -> None:
    """Half in use is not half deletable -- the folder is one unit."""
    roms, snes = _library(tmp_path)
    (snes / "imgs").mkdir()
    (snes / "imgs" / "Used.png").write_bytes(b"art in use")
    (snes / "imgs" / "Unused.png").write_bytes(b"art nothing points at")
    (snes / "gamelist.xml").write_text(
        '<?xml version="1.0"?>\n<gameList>\n'
        "  <game><path>./Game.sfc</path><image>./imgs/Used.png</image></game>\n"
        "</gameList>\n"
    )
    report = _verify(roms, tmp_path / "c.db")

    assert report.systems[0].legacy_media == []


def test_an_unreadable_gamelist_protects_legacy_media(tmp_path: Path) -> None:
    """Can't tell what's in use, so nothing is removable."""
    roms, snes = _library(tmp_path)
    (snes / "imgs").mkdir()
    (snes / "imgs" / "Game.png").write_bytes(b"art")
    (snes / "gamelist.xml").write_text("<gameList><game><path>truncated")
    report = _verify(roms, tmp_path / "c.db")

    assert report.systems[0].legacy_media == []
    assert (snes / "imgs" / "Game.png").exists()


def test_an_empty_leftover_folder_counts_as_legacy_media(tmp_path: Path) -> None:
    """Nothing in it to lose, and this system files its ROMs at the top level."""
    roms, snes = _library(tmp_path)
    (snes / "manual").mkdir()
    report = _verify(roms, tmp_path / "c.db")

    assert [folder.name for folder, _files in report.systems[0].legacy_media] == ["manual"]

    clean_orphans(report, apply=True, writer=BatoceraWriter())
    assert not (snes / "manual").exists()


def test_an_empty_folder_is_kept_when_roms_live_in_subfolders(tmp_path: Path) -> None:
    """An A-to-Z library owns nothing under Q yet. That is structure, not residue."""
    roms = tmp_path / "roms"
    gba = roms / "gba"
    (gba / "A").mkdir(parents=True)
    (gba / "A" / "Astro.gba").write_bytes(b"rom")
    (gba / "Q").mkdir()  # empty letter
    report = _verify(roms, tmp_path / "c.db")

    assert report.systems[0].legacy_media == []
    assert (gba / "Q").exists()


def test_a_folder_of_roms_is_never_legacy_media(tmp_path: Path) -> None:
    """The property that matters most: ROM folders are not art folders."""
    roms = tmp_path / "roms"
    gba = roms / "gba"
    (gba / "A").mkdir(parents=True)
    (gba / "A" / "Astro.gba").write_bytes(b"rom")
    report = _verify(roms, tmp_path / "c.db")

    assert report.systems[0].legacy_media == []
    assert (gba / "A" / "Astro.gba").exists()


def test_clean_orphans_removes_legacy_media_folders(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    legacy = snes / "imgs"
    legacy.mkdir()
    (legacy / "Game.png").write_bytes(b"art")
    report = _verify(roms, tmp_path / "c.db")

    media, entries, errors = clean_orphans(report, apply=True, writer=BatoceraWriter())

    assert errors == []
    assert media == 1
    assert entries == 0
    assert not legacy.exists()
    # The writer's own layout survives untouched.
    assert (snes / "images" / "Game-image.png").exists()


def test_clean_orphans_without_apply_leaves_legacy_media_alone(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    legacy = snes / "imgs"
    legacy.mkdir()
    (legacy / "Game.png").write_bytes(b"art")
    report = _verify(roms, tmp_path / "c.db")

    media, entries, errors = clean_orphans(report, apply=False, writer=BatoceraWriter())

    assert (media, entries, errors) == (1, 0, [])
    assert legacy.exists()


def test_no_rehash_skips_drift_detection(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    _verify(roms, tmp_path / "c.db")
    (snes / "Game.sfc").write_bytes(b"changed")

    report = _verify(roms, tmp_path / "c.db", rehash=False)
    assert report.drifted == 0
