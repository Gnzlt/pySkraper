"""Scanning. The hazard is media folders: walking them would try to scrape
thumbnails as if they were games."""

from __future__ import annotations

from pathlib import Path

from pyskraper.core.scanner import scan_system, scan_tree


def _make_library(root: Path) -> Path:
    snes = root / "snes"
    (snes / "images").mkdir(parents=True)
    (snes / "videos").mkdir()
    (snes / "Super Mario World.sfc").write_bytes(b"rom")
    (snes / "Zelda.smc").write_bytes(b"rom")
    (snes / "images" / "Super Mario World-image.png").write_bytes(b"png")
    (snes / "videos" / "Super Mario World-video.mp4").write_bytes(b"mp4")
    (snes / "gamelist.xml").write_text("<gameList/>")
    (snes / ".DS_Store").write_bytes(b"junk")
    (snes / "notes.txt").write_text("hi")
    return root


def test_finds_roms_and_ignores_media_and_metadata(tmp_path: Path) -> None:
    _make_library(tmp_path)
    scanned = scan_system(tmp_path / "snes")
    names = sorted(p.name for p in scanned.roms)
    assert names == ["Super Mario World.sfc", "Zelda.smc"]


def test_unknown_folder_is_reported_not_scraped(tmp_path: Path) -> None:
    (tmp_path / "definitely-not-a-console").mkdir()
    (tmp_path / "definitely-not-a-console" / "thing.bin").write_bytes(b"x")
    scanned = scan_system(tmp_path / "definitely-not-a-console")
    assert not scanned.is_known
    assert scanned.roms == []


def test_bin_track_beside_a_cue_is_not_a_game(tmp_path: Path) -> None:
    """A .bin is a track; the .cue is what the front-end lists. Scraping the
    track would double-count the game and waste a lookup on it."""
    psx = tmp_path / "psx"
    psx.mkdir()
    (psx / "Final Fantasy VII.cue").write_text("FILE ...")
    (psx / "Final Fantasy VII.bin").write_bytes(b"track")
    scanned = scan_system(psx)
    assert [p.name for p in scanned.roms] == ["Final Fantasy VII.cue"]


def test_standalone_bin_is_still_a_game(tmp_path: Path) -> None:
    md = tmp_path / "megadrive"
    md.mkdir()
    (md / "Sonic.bin").write_bytes(b"rom")
    assert [p.name for p in scan_system(md).roms] == ["Sonic.bin"]


def test_include_restricts_and_exclude_filters(tmp_path: Path) -> None:
    for folder in ("snes", "nes", "ps2"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "game.sfc").write_bytes(b"x")

    only_snes = scan_tree(tmp_path, include=["snes"], exclude=[])
    assert [s.folder for s in only_snes] == ["snes"]

    without_ps2 = scan_tree(tmp_path, include=[], exclude=["ps2"])
    assert "ps2" not in [s.folder for s in without_ps2]


def test_system_aliases_resolve(tmp_path: Path) -> None:
    (tmp_path / "genesis").mkdir()
    (tmp_path / "genesis" / "Sonic.md").write_bytes(b"x")
    scanned = scan_system(tmp_path / "genesis")
    assert scanned.is_known
    assert scanned.systeme_id == 1
