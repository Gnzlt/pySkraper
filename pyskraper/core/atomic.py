"""Atomic file writes.

Every write in this project goes through here.  The target is a removable SD
card: it can be pulled from the reader at any instant, and it has no Trash to
recover from.  A partially-written ``gamelist.xml`` costs a user their whole
library's metadata, and a truncated PNG shows up as a corrupt tile on the
handheld long after the run that caused it.

The pattern is always: write ``<target>.part``, ``fsync`` it, then
``os.replace`` onto the target.  ``os.replace`` is atomic within a filesystem,
so a reader sees either the old file or the new one, never a half-written one.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TextIO

__all__ = ["atomic_binary", "atomic_bytes", "atomic_copy", "atomic_text", "atomic_text_writer", "part_path"]

CHUNK = 1024 * 1024


def part_path(target: Path) -> Path:
    """The scratch name used while writing ``target``."""
    return target.with_name(target.name + ".part")


def _open_part(tmp: Path, flags: int, mode: int | None) -> int:
    """Open the part file, applying ``mode`` from the moment it exists.

    Creating the file and tightening it afterwards would leave a window where
    a secret is world-readable.  ``os.open`` takes the mode up front; the
    umask can only ever remove bits, so it is also applied explicitly.
    """
    if mode is None:
        return os.open(tmp, flags)
    descriptor = os.open(tmp, flags, mode)
    os.fchmod(descriptor, mode)
    return descriptor


@contextmanager
def atomic_binary(target: Path, *, mode: int | None = None) -> Iterator[BinaryIO]:
    """Write bytes to ``target`` atomically, optionally at permissions ``mode``."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = part_path(target)
    try:
        descriptor = _open_part(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "wb") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)


@contextmanager
def atomic_text_writer(target: Path, *, encoding: str = "utf-8", mode: int | None = None) -> Iterator[TextIO]:
    """Write text to ``target`` atomically, optionally at permissions ``mode``."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = part_path(target)
    try:
        descriptor = _open_part(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)


def atomic_copy(source: Path, target: Path, *, mode: int | None = None) -> Path:
    """Copy ``source`` onto ``target`` atomically, verifying the length.

    The size check is what separates this from ``shutil.copyfile``: a short
    read has to fail *before* the replace, so the target keeps whatever it had
    rather than becoming a truncated file that looks fine until something opens
    it.  The part file is cleaned up on any exception, cancellation included.
    """
    source = Path(source)
    expected = source.stat().st_size
    with atomic_binary(target, mode=mode) as handle:
        with open(source, "rb") as src:
            shutil.copyfileobj(src, handle, length=CHUNK)
        if handle.tell() != expected:
            raise OSError(f"short copy of {source}: {handle.tell()} of {expected} bytes")
    return Path(target)


def atomic_bytes(target: Path, data: bytes, *, mode: int | None = None) -> Path:
    with atomic_binary(target, mode=mode) as handle:
        handle.write(data)
    return Path(target)


def atomic_text(target: Path, text: str, *, mode: int | None = None) -> Path:
    with atomic_text_writer(target, mode=mode) as handle:
        handle.write(text)
    return Path(target)
