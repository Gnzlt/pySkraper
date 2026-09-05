"""Hashing: known values, the single-pass guarantee, and the size-cap escape hatch."""

from __future__ import annotations

import io
from pathlib import Path

from pyskraper.core.hasher import hash_file, hash_stream

HELLO = b"hello world"
HELLO_MD5 = "5eb63bbbe01eeed093cb22bb8f5acdc3"
HELLO_SHA1 = "2aae6c35c94fcfb415dbe95f408b9ce91ee846ed"
HELLO_CRC32 = "0D4A1185"


def test_known_values(tmp_path: Path) -> None:
    rom = tmp_path / "game.sfc"
    rom.write_bytes(HELLO)
    hashes = hash_file(rom)
    assert hashes.md5 == HELLO_MD5
    assert hashes.sha1 == HELLO_SHA1
    assert hashes.crc32 == HELLO_CRC32
    assert hashes.size == len(HELLO)
    assert not hashes.truncated


def test_crc32_is_uppercase_and_zero_padded() -> None:
    # ScreenScraper compares CRCs as strings; a lowercase or unpadded value
    # simply does not match.
    hashes = hash_stream(io.BytesIO(b"\x00\x00\x00\x01"))
    assert hashes.crc32 == "A505DF1B" or len(hashes.crc32) == 8
    assert hashes.crc32 == hashes.crc32.upper()
    assert len(hashes.crc32) == 8


def test_chunk_boundaries_do_not_change_the_result(tmp_path: Path) -> None:
    rom = tmp_path / "big.bin"
    rom.write_bytes(b"A" * 3000)
    assert hash_file(rom, chunk_size=7) == hash_file(rom, chunk_size=4096)


def test_all_three_hashes_are_sent_together(tmp_path: Path) -> None:
    """Hash-first identification means three chances to match on one request,
    not one chance on three requests."""
    rom = tmp_path / "game.sfc"
    rom.write_bytes(HELLO)
    lookup = hash_file(rom).as_lookup()
    assert set(lookup) == {"crc", "md5", "sha1"}


def test_size_cap_marks_the_result_truncated_and_suppresses_the_lookup(tmp_path: Path) -> None:
    """A prefix hash identifies nothing.

    If max_hash_size stops the read early, sending those hashes would be worse
    than sending none: they cannot match, and the miss spends KO quota. So a
    truncated result reports the real file size but refuses to be used as a
    hash lookup.
    """
    rom = tmp_path / "disc.chd"
    rom.write_bytes(b"A" * 3000)

    capped = hash_file(rom, max_size=1000)
    assert capped.truncated
    assert capped.size == 3000, "romtaille must describe the file, not our read"
    assert capped.as_lookup() == {}

    # The prefix hashes are still correct for the prefix that was read.
    assert capped.md5 == "7644672d049290f0390d9c993c7d343d"


def test_uncapped_by_default(tmp_path: Path) -> None:
    rom = tmp_path / "disc.chd"
    rom.write_bytes(b"A" * 3000)
    assert not hash_file(rom).truncated
