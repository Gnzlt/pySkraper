"""Reading the device off a mounted card.

The whole feature is a guess the user then confirms, so the bar here is not
"always right" -- it is "never confidently wrong, and never in the way".  Most
of these tests are therefore about the failure modes: an unreadable partition, a
board this version has not heard of, a file that turned out to be something
else.  All of them must come back as "not detected", never an exception.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from pyskraper.detect import BOARD_TO_PROFILES, board_sources, detect_profiles, read_board
from pyskraper.devices import PROFILES

MIRROR = ("system", "configs", "batteryplus", "knulli.board")
LOG = ("system", "logs", "knulli.log")
CANONICAL = ("boot", "knulli.board")


def _write(root: Path, parts: tuple[str, ...], text: str) -> Path:
    target = root.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def test_the_mirror_on_the_userdata_partition_is_read(tmp_path: Path) -> None:
    _write(tmp_path, MIRROR, "rg35xx-plus\n")
    assert read_board(tmp_path) == "rg35xx-plus"


def test_the_updater_log_is_read_when_the_mirror_is_missing(tmp_path: Path) -> None:
    _write(tmp_path, LOG, "Board: trimui-brick\nCurrent version: scarab\nChannel: stable\n")
    assert read_board(tmp_path) == "trimui-brick"


def test_the_log_reports_the_most_recent_board(tmp_path: Path) -> None:
    """The updater appends, so an old entry must not outrank the current one.

    This is the case where a card is moved between two handhelds: the log keeps
    both, and only the last one describes the hardware it is in now.
    """
    _write(tmp_path, LOG, "Board: rg35xx-plus\nChannel: stable\nBoard: rg40xx-h\nChannel: stable\n")
    assert read_board(tmp_path) == "rg40xx-h"


def test_the_boot_partition_outranks_the_mirror(tmp_path: Path) -> None:
    """The mirror is a copy; the boot file is the thing being copied."""
    volume, boot = tmp_path / "SHARE", tmp_path / "KNULLI"
    _write(volume, MIRROR, "rg35xx-plus")
    _write(boot, CANONICAL, "rg40xx-v")
    assert read_board(volume, boot) == "rg40xx-v"


def test_the_search_order_is_canonical_then_mirror_then_log(tmp_path: Path) -> None:
    volume, boot = tmp_path / "SHARE", tmp_path / "KNULLI"
    assert board_sources(volume, boot) == [
        boot.joinpath(*CANONICAL),
        volume.joinpath(*MIRROR),
        volume.joinpath(*LOG),
    ]


def test_a_card_with_no_boot_partition_skips_that_source(tmp_path: Path) -> None:
    assert all("KNULLI" not in str(p) for p in board_sources(tmp_path))


def test_an_empty_card_is_not_detected(tmp_path: Path) -> None:
    assert read_board(tmp_path) is None
    assert detect_profiles(tmp_path) == ()


def test_an_unreadable_source_falls_through_rather_than_raising(tmp_path: Path) -> None:
    """Exactly what happens on macOS: the boot partition answers EPERM.

    Measured on a real card -- an msdos volume mounted by fskit refuses every
    read, so `KNULLI/boot/knulli.board` fails even though the file is there.
    Falling through to the mirror is the only reason the feature works at all.
    """
    volume, boot = tmp_path / "SHARE", tmp_path / "KNULLI"
    denied = _write(boot, CANONICAL, "rg40xx-v")
    denied.chmod(0o000)
    _write(volume, MIRROR, "rg35xx-plus")
    try:
        assert read_board(volume, boot) == "rg35xx-plus"
    finally:
        denied.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 directory anyway")
def test_an_unreadable_volume_is_a_miss_not_an_error(tmp_path: Path) -> None:
    volume = tmp_path / "SHARE"
    _write(volume, MIRROR, "rg35xx-plus")
    volume.chmod(0o000)
    try:
        assert read_board(volume) is None
    finally:
        volume.chmod(stat.S_IRWXU)


@pytest.mark.parametrize("junk", ["", "   \n", "not a board name", "../../etc/passwd", "RG35XX-Plus", "x" * 200])
def test_a_file_that_is_not_a_board_name_is_ignored(tmp_path: Path, junk: str) -> None:
    """Better to ask the user than to act on whatever happened to be in there."""
    _write(tmp_path, MIRROR, junk)
    assert read_board(tmp_path) is None


def test_a_huge_file_is_rejected_rather_than_parsed(tmp_path: Path) -> None:
    """Pointing at the wrong file should cost a bounded read and nothing else."""
    _write(tmp_path, MIRROR, "z" * 5_000_000)
    assert read_board(tmp_path) is None


def test_a_known_board_resolves_to_its_profile(tmp_path: Path) -> None:
    _write(tmp_path, MIRROR, "trimui-brick")
    (found,) = detect_profiles(tmp_path)
    assert found.id == "trimui-brick"
    assert found.screen == (1024, 768)


def test_an_unmapped_board_detects_nothing(tmp_path: Path) -> None:
    """A board newer than this mapping means "ask", not "crash" or "guess"."""
    _write(tmp_path, MIRROR, "rg99xx-quantum")
    assert read_board(tmp_path) == "rg99xx-quantum"
    assert detect_profiles(tmp_path) == ()


def test_the_shared_anbernic_board_offers_both_models(tmp_path: Path) -> None:
    """One image covers the RG35XX 2024 and the Plus, so the card cannot choose.

    Both are 640x480, so the artwork size is right either way -- which is why
    offering the first and naming the other is honest rather than a coin toss.
    """
    _write(tmp_path, MIRROR, "rg35xx-plus")
    found = detect_profiles(tmp_path)
    assert [p.id for p in found] == ["anbernic-rg35xx-2024", "anbernic-rg35xx-plus"]
    assert {p.screen for p in found} == {(640, 480)}


def test_a_retroid_card_before_first_boot_is_not_guessed(tmp_path: Path) -> None:
    """`sm8250` is four devices with three screen sizes until KNULLI rewrites it."""
    _write(tmp_path, MIRROR, "sm8250")
    assert detect_profiles(tmp_path) == ()


def test_every_mapped_board_names_a_real_profile() -> None:
    for board, ids in BOARD_TO_PROFILES.items():
        assert ids, f"{board!r} maps to nothing"
        for name in ids:
            assert name in PROFILES, f"{board!r} maps to unknown profile {name!r}"


def test_every_device_is_reachable_by_detection() -> None:
    """The roster and the mapping have to stay the same set.

    A profile no board names is a device the card can never be recognised as,
    which is the whole point of the roster covering every board KNULLI ships an
    image for.  Adding one without its board name silently loses that.
    """
    claimed = {name for ids in BOARD_TO_PROFILES.values() for name in ids}
    assert claimed == set(PROFILES) - {"none"}


def test_no_profile_is_claimed_by_two_boards() -> None:
    """Two boards resolving to one device would make `doctor` ambiguous."""
    claimed = [name for ids in BOARD_TO_PROFILES.values() for name in ids]
    assert len(claimed) == len(set(claimed))
