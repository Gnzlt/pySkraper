"""Atomicity. The target is a removable card with no Trash, so a failed write
must leave the previous file intact and no .part litter behind."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from pyskraper.core.atomic import (
    atomic_binary,
    atomic_bytes,
    atomic_copy,
    atomic_text,
    atomic_text_writer,
    part_path,
)


def test_write_creates_the_file(tmp_path: Path) -> None:
    target = tmp_path / "images" / "game-image.png"
    atomic_bytes(target, b"PNGDATA")
    assert target.read_bytes() == b"PNGDATA"


def test_parent_directories_are_created(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.txt"
    atomic_text(target, "hi")
    assert target.exists()


def test_failure_midwrite_leaves_the_previous_file_untouched(tmp_path: Path) -> None:
    """The important case: a card pulled mid-run must not corrupt gamelist.xml."""
    target = tmp_path / "gamelist.xml"
    target.write_text("<gameList>original</gameList>")

    with pytest.raises(RuntimeError), atomic_binary(target) as handle:
        handle.write(b"half a file")
        raise RuntimeError("card yanked")

    assert target.read_text() == "<gameList>original</gameList>"


def test_failure_removes_the_part_file(tmp_path: Path) -> None:
    target = tmp_path / "game-image.png"
    with pytest.raises(RuntimeError), atomic_binary(target) as handle:
        handle.write(b"partial")
        raise RuntimeError("boom")

    assert not part_path(target).exists(), "a failed write must not leave .part litter on the card"
    assert not target.exists()


def test_target_never_observed_partially_written(tmp_path: Path) -> None:
    target = tmp_path / "big.bin"
    target.write_bytes(b"old")
    with atomic_binary(target) as handle:
        handle.write(b"new content")
        # Until the context manager exits, readers still see the old file.
        assert target.read_bytes() == b"old"
    assert target.read_bytes() == b"new content"


def test_mode_applies_to_the_part_file_not_just_the_target(tmp_path: Path) -> None:
    """The config holds two passwords.

    Creating the part file at the default mode and tightening it after the
    replace would leave a window where the secrets are world-readable, so the
    permissions have to be right from the moment the file exists.
    """
    target = tmp_path / "pyskraper.yaml"
    seen: list[int] = []

    with atomic_text_writer(target, mode=0o600) as handle:
        handle.write("screenscraper:\n  devpassword: hunter2\n")
        seen.append(stat.S_IMODE(part_path(target).stat().st_mode))

    assert seen == [0o600], "the part file was readable before the replace"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_mode_is_optional(tmp_path: Path) -> None:
    target = tmp_path / "gamelist.xml"
    atomic_text(target, "<gameList />")
    assert target.read_text() == "<gameList />"


def test_atomic_copy_copies_content(tmp_path: Path) -> None:
    source = tmp_path / "box-2D.png"
    source.write_bytes(b"PNGDATA" * 1000)
    target = tmp_path / "images" / "Game-thumb.png"

    atomic_copy(source, target)

    assert target.read_bytes() == source.read_bytes()
    assert not part_path(target).exists()


def test_atomic_copy_failure_leaves_no_part_and_no_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A short copy must fail before the replace.

    Publishing a truncated image puts a file on the card that looks fine until
    the theme tries to render it, long after the run that caused it.
    """
    source = tmp_path / "box-2D.png"
    source.write_bytes(b"PNGDATA" * 1000)
    target = tmp_path / "images" / "Game-thumb.png"

    def short_copy(src: object, dst: Any, length: int = 0) -> None:
        dst.write(b"trunc")

    monkeypatch.setattr(shutil, "copyfileobj", short_copy)

    with pytest.raises(OSError, match="short copy"):
        atomic_copy(source, target)

    assert not part_path(target).exists()
    assert not target.exists()
    assert source.exists(), "the source must never be at risk"


def test_atomic_copy_does_not_clobber_on_failure(tmp_path: Path) -> None:
    source = tmp_path / "new.png"
    source.write_bytes(b"new bytes")
    target = tmp_path / "existing.png"
    target.write_bytes(b"the file already on the card")

    with pytest.raises(OSError):
        atomic_copy(tmp_path / "missing.png", target)

    assert target.read_bytes() == b"the file already on the card"
