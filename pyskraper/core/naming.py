"""Filename cleaning, and the cataloguing tags underneath it.

`clean_name` is used at the very end of the identification chain. Cataloguing
conventions (No-Intro, GoodTools, TOSEC) bolt region, revision and dump-status
markers onto filenames, and none of them belong in a search term -- "Super Mario
World (USA) [!]" finds nothing, "Super Mario World" finds the game.

`parse_tags` reads those same markers instead of discarding them, because
choosing which of five copies of one game to keep is exactly the question they
answer.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

__all__ = ["FileTags", "clean_name", "parse_tags", "similarity"]

# (USA), (Europe), (Rev 1), (v1.1), (Disc 1), [!], [a1], [T+Eng] ...
_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_SEPARATORS = re.compile(r"[_\.]+")
_WHITESPACE = re.compile(r"\s+")

# "Legend of Zelda, The" -> "The Legend of Zelda"
_TRAILING_ARTICLE = re.compile(r"^(.*),\s*(The|A|An|Le|La|Les|Der|Die|Das|El|Los)$", re.IGNORECASE)


def _without_extension(filename: str) -> str:
    """The filename with its final suffix removed, if it has one.

    Not `Path.stem`: these are bare ROM names, and a title containing a dot
    ("Mr. Do!") must not lose the part after it when there is no extension.
    """
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def clean_name(filename: str) -> str:
    """Reduce a ROM filename to a searchable title."""
    name = _without_extension(filename)

    name = _BRACKETED.sub(" ", name)
    name = _SEPARATORS.sub(" ", name)
    # A hyphen is a word separator in "Sonic-The-Hedgehog" but part of the title
    # in "Spider-Man", so only split it when it is surrounded by spaces.
    name = re.sub(r"\s+-\s+", " ", name)
    name = _WHITESPACE.sub(" ", name).strip(" -")

    match = _TRAILING_ARTICLE.match(name)
    if match:
        name = f"{match.group(2)} {match.group(1)}".strip()

    return name


def similarity(left: str, right: str) -> float:
    """Rough title similarity in ``0.0..1.0``, used to rank search candidates."""
    return difflib.SequenceMatcher(None, left.lower().strip(), right.lower().strip()).ratio()


# --------------------------------------------------------------------------
# Cataloguing tags
# --------------------------------------------------------------------------
#
# The same bracketed markers `clean_name` throws away are exactly what dedupe
# needs to keep: which of five copies of one game is the USA release, which is
# the latest revision, which is a verified good dump.  Parsing them here rather
# than in `dedupe` keeps every filename convention in one module.

# Token inside brackets -> region code, matching `preferences.region` spelling.
_REGION_TOKENS: dict[str, str] = {
    "usa": "us",
    "us": "us",
    "u": "us",
    "america": "us",
    "canada": "ca",
    "europe": "eu",
    "eur": "eu",
    "e": "eu",
    "japan": "jp",
    "jpn": "jp",
    "j": "jp",
    "world": "wor",
    "w": "wor",
    "germany": "de",
    "france": "fr",
    "spain": "sp",
    "italy": "it",
    "netherlands": "nl",
    "sweden": "se",
    "korea": "kr",
    "china": "cn",
    "taiwan": "tw",
    "brazil": "br",
    "australia": "au",
    "asia": "asi",
    "russia": "ru",
    "hong kong": "hk",
}

# Anything that means "this is not the finished game".
_PRERELEASE = frozenset({"beta", "proto", "prototype", "sample", "demo", "alpha", "preview"})

_REV = re.compile(r"^rev[\s\-]*([0-9]+|[a-z])$", re.IGNORECASE)
_VERSION = re.compile(r"^v[\s\-]*([0-9]+)(?:\.([0-9]+))?$", re.IGNORECASE)
_PRG = re.compile(r"^prg[\s\-]*([0-9]+)$", re.IGNORECASE)
_BRACKET_GROUP = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")


@dataclass(frozen=True)
class FileTags:
    """What a cataloguing convention says about one ROM filename."""

    regions: frozenset[str] = frozenset()
    revision: float = 0.0
    """Higher is later. Negative means a pre-release (beta, proto, sample)."""
    verified: bool = False
    """GoodTools `[!]` -- a dump checked against a known-good checksum."""
    bad_dump: bool = False
    """`[b]` -- a known-corrupt dump. Never a good survivor."""
    translated: bool = False
    """`[T+Eng]`, `[T-Fre]` -- a fan translation, not an original release."""


def parse_tags(filename: str) -> FileTags:
    """Read the region/revision/dump markers out of a ROM filename.

    Deliberately tolerant: an unrecognised marker contributes nothing rather
    than raising, because these conventions are informal and endless.
    """
    stem = _without_extension(filename)

    regions: set[str] = set()
    revision = 0.0
    prerelease = False
    verified = False
    bad_dump = False
    translated = False

    for group in _BRACKET_GROUP.findall(stem):
        body = group.strip()
        if not body:
            continue

        lowered = body.lower()
        if lowered == "!":
            verified = True
            continue
        if lowered in ("b", "o") or lowered.startswith(("b0", "b1", "b2", "o0", "o1")):
            bad_dump = True
            continue
        if lowered.startswith(("t+", "t-")):
            translated = True
            continue

        for token in re.split(r"[,+/]", body):
            piece = token.strip().lower()
            if not piece:
                continue
            if piece in _REGION_TOKENS:
                regions.add(_REGION_TOKENS[piece])
                continue
            if piece in _PRERELEASE:
                prerelease = True
                continue

            match = _REV.match(piece)
            if match:
                revision = max(revision, _rank(match.group(1)))
                continue
            match = _VERSION.match(piece)
            if match:
                minor = int(match.group(2) or 0)
                revision = max(revision, float(match.group(1)) + minor / 1000.0)
                continue
            match = _PRG.match(piece)
            if match:
                revision = max(revision, float(match.group(1)))

    return FileTags(
        regions=frozenset(regions),
        # A prototype outranks nothing, whatever revision number it carries --
        # and it must not depend on which marker the filename happens to list first.
        revision=-1.0 if prerelease else revision,
        verified=verified,
        bad_dump=bad_dump,
        translated=translated,
    )


def _rank(token: str) -> float:
    """`Rev 2` -> 2.0, `Rev B` -> 2.0. The two conventions are interchangeable."""
    if token.isdigit():
        return float(token)
    return float(ord(token.lower()) - ord("a") + 1)
