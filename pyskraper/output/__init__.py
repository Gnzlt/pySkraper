"""Output writers.  Importing this package registers every built-in format."""

from __future__ import annotations

from .base import EntryInfo, Writer, get_writer, register_writer
from .batocera import BatoceraWriter

__all__ = ["BatoceraWriter", "EntryInfo", "Writer", "get_writer", "register_writer"]
