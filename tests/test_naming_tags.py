"""Cataloguing-tag parsing.

These markers decide which copy of a game survives a dedupe, so a wrong reading
here is a wrong deletion later.
"""

from __future__ import annotations

import pytest

from pyskraper.core.naming import parse_tags


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Super Mario World (USA).sfc", {"us"}),
        ("Super Mario World (Europe).sfc", {"eu"}),
        ("Super Mario World (Japan, USA).sfc", {"jp", "us"}),
        ("Sonic (World).md", {"wor"}),
        ("Chrono Trigger (U) [!].sfc", {"us"}),
        ("Homebrew Thing.nes", set()),
    ],
)
def test_reads_regions(filename: str, expected: set[str]) -> None:
    assert set(parse_tags(filename).regions) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Game.sfc", 0.0),
        ("Game (Rev 1).sfc", 1.0),
        ("Game (Rev 2).sfc", 2.0),
        ("Game (Rev A).sfc", 1.0),
        ("Game (Rev B).sfc", 2.0),
        ("Game (PRG1).nes", 1.0),
    ],
)
def test_ranks_revisions(filename: str, expected: float) -> None:
    assert parse_tags(filename).revision == expected


def test_letter_and_number_revisions_are_interchangeable() -> None:
    assert parse_tags("Game (Rev B).sfc").revision == parse_tags("Game (Rev 2).sfc").revision


def test_versions_order_correctly() -> None:
    assert parse_tags("Game (v1.1).sfc").revision > parse_tags("Game (v1.0).sfc").revision
    assert parse_tags("Game (v2.0).sfc").revision > parse_tags("Game (v1.9).sfc").revision


def test_a_prototype_never_outranks_a_release() -> None:
    assert parse_tags("Game (Proto).sfc").revision < parse_tags("Game.sfc").revision
    assert parse_tags("Game (Beta).sfc").revision < parse_tags("Game.sfc").revision


def test_prerelease_wins_regardless_of_marker_order() -> None:
    """A `(Rev 1) (Beta)` must not read as revision 1 just because Rev came first."""
    assert parse_tags("Game (Rev 1) (Beta).sfc").revision < 0
    assert parse_tags("Game (Beta) (Rev 1).sfc").revision < 0


def test_reads_dump_status() -> None:
    assert parse_tags("Game (U) [!].sfc").verified
    assert parse_tags("Game (U) [b].sfc").bad_dump
    assert parse_tags("Game (J) [T+Eng1.0].sfc").translated
    assert not parse_tags("Game (U).sfc").verified


def test_unknown_markers_are_ignored_rather_than_raising() -> None:
    tags = parse_tags("Game (Unl) (Aftermarket) [x][?].sfc")
    assert tags.regions == frozenset()
    assert tags.revision == 0.0
