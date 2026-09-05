"""Containers and disc images: the cases where 'just hash the file' is wrong."""

from __future__ import annotations

import zipfile
from pathlib import Path

from pyskraper.core.archives import candidate_hashes, hash_archive_contents, is_archive
from pyskraper.core.hasher import hash_file
from pyskraper.core.serials import extract_serial, resolve_disc_target
from pyskraper.systems import lookup

ROM_BYTES = b"INNER ROM CONTENT" * 100


def _zip_with(tmp_path: Path, members: dict[str, bytes], name: str = "Game.zip") -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        for filename, data in members.items():
            zf.writestr(filename, data)
    return archive


def test_is_archive() -> None:
    assert is_archive(Path("a.zip")) and is_archive(Path("a.7z"))
    assert not is_archive(Path("a.sfc"))


def test_inner_rom_is_hashed_not_the_archive(tmp_path: Path) -> None:
    """No-Intro catalogues the raw ROM, so a zipped SNES game must be matched
    on its contents. Hashing the zip is a guaranteed miss."""
    archive = _zip_with(tmp_path, {"Game.sfc": ROM_BYTES})
    raw = tmp_path / "Game.sfc"
    raw.write_bytes(ROM_BYTES)

    inner = hash_archive_contents(archive)
    assert inner is not None
    assert inner.md5 == hash_file(raw).md5
    assert inner.md5 != hash_file(archive).md5


def test_documentation_inside_the_archive_is_ignored(tmp_path: Path) -> None:
    archive = _zip_with(tmp_path, {"readme.txt": b"hello", "Game.sfc": ROM_BYTES})
    raw = tmp_path / "Game.sfc"
    raw.write_bytes(ROM_BYTES)
    inner = hash_archive_contents(archive)
    assert inner is not None and inner.md5 == hash_file(raw).md5


def test_sevenzip_returns_none_rather_than_guessing(tmp_path: Path) -> None:
    """We do not decode .7z. The honest answer is None, so the chain falls back
    to hashing the archive rather than silently mis-hashing."""
    fake = tmp_path / "Game.7z"
    fake.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"junk")
    assert hash_archive_contents(fake) is None


def test_corrupt_zip_does_not_raise(tmp_path: Path) -> None:
    broken = tmp_path / "Game.zip"
    broken.write_bytes(b"not really a zip")
    assert hash_archive_contents(broken) is None


def test_console_policy_prefers_contents(tmp_path: Path) -> None:
    snes = lookup("snes")
    assert snes is not None
    archive = _zip_with(tmp_path, {"Game.sfc": ROM_BYTES})
    sources = [source for source, _ in candidate_hashes(archive, snes, policy="auto")]
    assert sources == ["contents"]


def test_arcade_policy_hashes_the_archive(tmp_path: Path) -> None:
    """MAME loads the zip itself, and ScreenScraper's arcade entries are keyed
    on it."""
    arcade = lookup("mame")
    assert arcade is not None
    archive = _zip_with(tmp_path, {"rom1.bin": ROM_BYTES})
    sources = [source for source, _ in candidate_hashes(archive, arcade, policy="auto")]
    assert sources == ["archive"]


def test_both_policy_tries_contents_first_for_consoles(tmp_path: Path) -> None:
    snes = lookup("snes")
    assert snes is not None
    archive = _zip_with(tmp_path, {"Game.sfc": ROM_BYTES})
    sources = [source for source, _ in candidate_hashes(archive, snes, policy="both")]
    assert sources == ["contents", "archive"]


def test_plain_file_yields_itself(tmp_path: Path) -> None:
    snes = lookup("snes")
    assert snes is not None
    rom = tmp_path / "Game.sfc"
    rom.write_bytes(ROM_BYTES)
    results = list(candidate_hashes(rom, snes))
    assert [s for s, _ in results] == ["file"]
    assert results[0][1].md5 == hash_file(rom).md5


def test_supplied_file_hashes_are_used_verbatim(tmp_path: Path) -> None:
    """A caller that already hashed the file does not pay for it twice.

    Identification computes these for cache keying immediately before asking
    for candidates, so re-reading the file here doubled the disk cost of every
    plain ROM on the card.
    """
    snes = lookup("snes")
    assert snes is not None
    rom = tmp_path / "Game.sfc"
    rom.write_bytes(ROM_BYTES)
    known = hash_file(rom)

    # Truncating the file would change any freshly computed hash. The supplied
    # one coming back unchanged is what proves nothing was re-read.
    rom.write_bytes(b"")
    results = list(candidate_hashes(rom, snes, file_hashes=known))

    assert [s for s, _ in results] == ["file"]
    assert results[0][1] == known


class TestDiscResolution:
    def test_cue_resolves_to_its_track(self, tmp_path: Path) -> None:
        """Hashing the text of a cue sheet identifies the text file, not the game."""
        (tmp_path / "FF7.bin").write_bytes(b"track data")
        cue = tmp_path / "FF7.cue"
        cue.write_text('FILE "FF7.bin" BINARY\n  TRACK 01 MODE2/2352\n')
        assert resolve_disc_target(cue).name == "FF7.bin"

    def test_m3u_resolves_to_its_first_disc(self, tmp_path: Path) -> None:
        (tmp_path / "FF7 (Disc 1).bin").write_bytes(b"d1")
        (tmp_path / "FF7 (Disc 1).cue").write_text('FILE "FF7 (Disc 1).bin" BINARY\n')
        (tmp_path / "FF7 (Disc 2).cue").write_text('FILE "nope.bin" BINARY\n')
        m3u = tmp_path / "FF7.m3u"
        m3u.write_text("FF7 (Disc 1).cue\nFF7 (Disc 2).cue\n")
        assert resolve_disc_target(m3u).name == "FF7 (Disc 1).bin"

    def test_missing_reference_falls_back_to_the_file_itself(self, tmp_path: Path) -> None:
        cue = tmp_path / "Broken.cue"
        cue.write_text('FILE "does-not-exist.bin" BINARY\n')
        assert resolve_disc_target(cue) == cue

    def test_plain_iso_is_its_own_target(self, tmp_path: Path) -> None:
        iso = tmp_path / "Game.iso"
        iso.write_bytes(b"x")
        assert resolve_disc_target(iso) == iso


class TestSerials:
    def test_extracts_a_playstation_serial(self, tmp_path: Path) -> None:
        iso = tmp_path / "Game.iso"
        iso.write_bytes(b"\x00" * 2048 + b"BOOT = cdrom:\\SLUS_949.01;1\r\n" + b"\x00" * 100)
        assert extract_serial(iso) == "SLUS-94901"

    def test_extracts_through_a_cue_sheet(self, tmp_path: Path) -> None:
        (tmp_path / "Game.bin").write_bytes(b"\x00" * 100 + b"BOOT = cdrom:\\SCES_003.11;1")
        cue = tmp_path / "Game.cue"
        cue.write_text('FILE "Game.bin" BINARY\n')
        assert extract_serial(cue) == "SCES-00311"

    def test_compressed_images_return_none_rather_than_a_guess(self, tmp_path: Path) -> None:
        """A wrong serial is worse than no serial: it would confidently scrape
        the wrong game. .chd needs a real decoder we do not have."""
        chd = tmp_path / "Game.chd"
        chd.write_bytes(b"MComprHD" + b"\x00" * 200)
        assert extract_serial(chd) is None

    def test_no_serial_present(self, tmp_path: Path) -> None:
        iso = tmp_path / "Game.iso"
        iso.write_bytes(b"\x00" * 5000)
        assert extract_serial(iso) is None

    def test_unreadable_file_is_not_fatal(self, tmp_path: Path) -> None:
        assert extract_serial(tmp_path / "missing.iso") is None
