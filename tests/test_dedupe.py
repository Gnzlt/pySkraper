"""Duplicate detection and the safety discipline around it.

This is the only code in the project that can destroy a user's ROM collection,
so the tests lean hard on the refusals: that a report changes nothing, that
conflicting rules skip a group rather than guess, and that a quarantine can be
undone byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pyskraper.core.cache import Cache
from pyskraper.core.dedupe import (
    Action,
    DedupeError,
    DuplicateKind,
    RomEntry,
    apply_plan,
    build_entries,
    build_rules,
    find_duplicates,
    plan_removals,
)
from pyskraper.core.naming import parse_tags
from pyskraper.core.scanner import scan_tree
from pyskraper.output.batocera import BatoceraWriter

KEEP_PRIORITY = ["region:us", "verified", "latest-revision", "in-gamelist", "shortest-name"]


def _entry(
    tmp_path: Path,
    name: str,
    *,
    system: str = "snes",
    md5: str | None = "aaa",
    game_id: int | None = None,
    in_gamelist: bool = False,
    size: int = 100,
) -> RomEntry:
    system_dir = tmp_path / system
    return RomEntry(
        path=system_dir / name,
        system=system,
        system_dir=system_dir,
        size=size,
        md5=md5,
        game_id=game_id,
        in_gamelist=in_gamelist,
        tags=parse_tags(name),
    )


# ---- keep rules ----------------------------------------------------------


def test_region_rule_picks_the_preferred_region(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "Game (Europe).sfc"), _entry(tmp_path, "Game (USA).sfc")]
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)

    group = report.groups[0]
    assert group.keeper is not None
    assert group.keeper.name == "Game (USA).sfc"


def test_later_revision_wins_when_region_cannot_decide(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "Game (USA).sfc"), _entry(tmp_path, "Game (USA) (Rev 1).sfc")]
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)

    assert report.groups[0].keeper is not None
    assert report.groups[0].keeper.name == "Game (USA) (Rev 1).sfc"


def test_conflicting_rules_skip_the_group_rather_than_guess(tmp_path: Path) -> None:
    """The preferred region is not the latest revision. Nothing is removed."""
    entries = [
        _entry(tmp_path, "Game (USA).sfc", md5=None, game_id=7),
        _entry(tmp_path, "Game (Europe) (Rev 1).sfc", md5=None, game_id=7),
    ]
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)

    group = report.groups[0]
    assert group.keeper is None
    assert group.skipped is not None
    assert "disagree" in group.skipped
    assert report.removals == []


def test_identical_files_fall_back_to_a_deterministic_tiebreak(tmp_path: Path) -> None:
    """Byte-identical content: no rule can distinguish, but nothing is lost either."""
    entries = [
        _entry(tmp_path, "sub/Game.sfc"),
        _entry(tmp_path, "Game.sfc"),
    ]
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)

    group = report.groups[0]
    assert group.keeper is not None
    assert group.keeper.name == "Game.sfc"  # shallowest path wins


def test_undecidable_same_game_group_is_skipped(tmp_path: Path) -> None:
    """Different content and nothing to tell them apart: never guess."""
    entries = [
        _entry(tmp_path, "Game.sfc", md5="a", game_id=7),
        _entry(tmp_path, "Gamf.sfc", md5="b", game_id=7),
    ]
    report = find_duplicates(entries, detect=["same-game"], keep_priority=KEEP_PRIORITY)

    assert report.groups[0].keeper is None
    assert report.removals == []


def test_shortest_name_breaks_ties_without_creating_conflicts(tmp_path: Path) -> None:
    """A revision marker always lengthens the filename.

    If `shortest-name` counted as evidence, it would contradict
    `latest-revision` on every group with a revision in it and the whole
    library would come back "undecidable". It is a tiebreak, not a claim.
    """
    entries = [_entry(tmp_path, "Game (USA).sfc"), _entry(tmp_path, "Game (USA) (Rev 1).sfc")]
    group = find_duplicates(entries, keep_priority=KEEP_PRIORITY).groups[0]

    assert group.skipped is None
    assert group.keeper is not None
    assert group.keeper.name == "Game (USA) (Rev 1).sfc"


def test_shortest_name_still_decides_when_nothing_substantive_applies(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path, "Game (USA) (Some Long Tag).sfc", md5=None, game_id=7),
        _entry(tmp_path, "Game (USA).sfc", md5=None, game_id=7),
    ]
    group = find_duplicates(entries, keep_priority=KEEP_PRIORITY).groups[0]

    assert group.keeper is not None
    assert group.keeper.name == "Game (USA).sfc"


def test_unknown_keep_rule_is_an_error_not_a_shrug() -> None:
    with pytest.raises(DedupeError, match="Unknown dedupe keep rule"):
        build_rules(["region:us", "biggest-filename"])


# ---- grouping ------------------------------------------------------------


def test_exact_and_same_game_are_reported_separately(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path, "Game (USA).sfc", md5="same", game_id=7),
        _entry(tmp_path, "Game copy (USA).sfc", md5="same", game_id=7),
        _entry(tmp_path, "Game (Japan).sfc", md5="other", game_id=7),
    ]
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)

    kinds = {group.kind for group in report.groups}
    assert kinds == {DuplicateKind.EXACT, DuplicateKind.SAME_GAME}


def test_a_file_is_never_proposed_for_removal_twice(tmp_path: Path) -> None:
    """An exact duplicate's loser must not reappear in a same-game group."""
    entries = [
        _entry(tmp_path, "Game (USA).sfc", md5="same", game_id=7),
        _entry(tmp_path, "Game copy (USA).sfc", md5="same", game_id=7),
        _entry(tmp_path, "Game (Japan).sfc", md5="other", game_id=7),
    ]
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)

    paths = [entry.path for entry in report.removals]
    assert len(paths) == len(set(paths))


def test_cross_system_duplicates_are_reported_but_never_actioned(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path, "Game (USA).sfc", system="snes", md5="same"),
        _entry(tmp_path, "Game (USA).sfc", system="sfc", md5="same"),
    ]
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)

    assert len(report.cross_system) == 1
    assert report.cross_system[0].removals == []
    assert report.removals == []


def test_a_unique_rom_is_not_a_duplicate(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "Game (USA).sfc", md5="a"), _entry(tmp_path, "Other (USA).sfc", md5="b")]
    assert find_duplicates(entries, keep_priority=KEEP_PRIORITY).groups == []


def test_unhashable_roms_are_left_out_of_exact_detection(tmp_path: Path) -> None:
    entries = [_entry(tmp_path, "A.sfc", md5=None), _entry(tmp_path, "B.sfc", md5=None)]
    assert find_duplicates(entries, detect=["exact"], keep_priority=KEEP_PRIORITY).groups == []


# ---- the fixture library -------------------------------------------------


def _library(tmp_path: Path) -> tuple[Path, Path]:
    """A snes folder with one exact duplicate pair, plus media for both."""
    roms = tmp_path / "roms"
    snes = roms / "snes"
    (snes / "images").mkdir(parents=True)

    (snes / "Game (USA).sfc").write_bytes(b"identical content")
    (snes / "Game (USA) [dup].sfc").write_bytes(b"identical content")
    (snes / "Other (USA).sfc").write_bytes(b"different")

    (snes / "images" / "Game (USA)-image.png").write_bytes(b"art-keep")
    (snes / "images" / "Game (USA) [dup]-image.png").write_bytes(b"art-dup")
    (snes / "images" / "Other (USA)-image.png").write_bytes(b"art-other")
    return roms, snes


def test_indexes_a_real_library(tmp_path: Path) -> None:
    roms, _snes = _library(tmp_path)
    with Cache(tmp_path / "c.db") as cache:
        entries = build_entries(scan_tree(roms), cache=cache, writer=BatoceraWriter())

    assert len(entries) == 3
    digest = hashlib.md5(b"identical content").hexdigest()
    assert sum(1 for entry in entries if entry.md5 == digest) == 2


def test_report_only_action_plans_nothing(tmp_path: Path) -> None:
    roms, _snes = _library(tmp_path)
    with Cache(tmp_path / "c.db") as cache:
        entries = build_entries(scan_tree(roms), cache=cache, writer=BatoceraWriter())
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)

    plan = plan_removals(
        report, entries=entries, action=Action.REPORT_ONLY, quarantine_dir=tmp_path / "q", writer=BatoceraWriter()
    )
    assert plan.removals == []


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    before = {path: path.read_bytes() for path in snes.rglob("*") if path.is_file()}

    with Cache(tmp_path / "c.db") as cache:
        entries = build_entries(scan_tree(roms), cache=cache, writer=BatoceraWriter())
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)
    plan = plan_removals(
        report, entries=entries, action=Action.QUARANTINE, quarantine_dir=tmp_path / "q", writer=BatoceraWriter()
    )
    outcome = apply_plan(plan, apply=False, journal_dir=tmp_path / "j", writer=BatoceraWriter())

    assert outcome.moved == 2  # the ROM and its image, counted but not moved
    assert {path: path.read_bytes() for path in snes.rglob("*") if path.is_file()} == before
    assert not (tmp_path / "q").exists()


def test_quarantine_moves_exactly_the_intended_files(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    quarantine = tmp_path / "q"

    with Cache(tmp_path / "c.db") as cache:
        entries = build_entries(scan_tree(roms), cache=cache, writer=BatoceraWriter())
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)
    plan = plan_removals(
        report, entries=entries, action=Action.QUARANTINE, quarantine_dir=quarantine, writer=BatoceraWriter()
    )
    apply_plan(plan, apply=True, journal_dir=tmp_path / "j", writer=BatoceraWriter())

    # The duplicate and its artwork are gone; everything else is untouched.
    assert not (snes / "Game (USA) [dup].sfc").exists()
    assert not (snes / "images" / "Game (USA) [dup]-image.png").exists()
    assert (snes / "Game (USA).sfc").read_bytes() == b"identical content"
    assert (snes / "images" / "Game (USA)-image.png").read_bytes() == b"art-keep"
    assert (snes / "Other (USA).sfc").exists()

    # And they are intact in quarantine, under the card's own layout.
    assert (quarantine / "snes" / "Game (USA) [dup].sfc").read_bytes() == b"identical content"
    assert (quarantine / "snes" / "images" / "Game (USA) [dup]-image.png").read_bytes() == b"art-dup"


def test_restoring_from_quarantine_returns_the_library_bit_for_bit(tmp_path: Path) -> None:
    """The milestone's exit criterion: a mistaken run is fully reversible."""
    roms, snes = _library(tmp_path)
    quarantine = tmp_path / "q"
    before = {path.relative_to(snes): path.read_bytes() for path in sorted(snes.rglob("*")) if path.is_file()}

    with Cache(tmp_path / "c.db") as cache:
        entries = build_entries(scan_tree(roms), cache=cache, writer=BatoceraWriter())
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)
    plan = plan_removals(
        report, entries=entries, action=Action.QUARANTINE, quarantine_dir=quarantine, writer=BatoceraWriter()
    )
    outcome = apply_plan(plan, apply=True, journal_dir=tmp_path / "j", writer=BatoceraWriter())

    # Replay the journal backwards, which is all a restore is.
    assert outcome.journal is not None
    for line in outcome.journal.read_text().splitlines():
        record = json.loads(line)
        assert record["action"] == "quarantine"
        Path(record["dst"]).rename(record["src"])

    after = {path.relative_to(snes): path.read_bytes() for path in sorted(snes.rglob("*")) if path.is_file()}
    assert after == before


def test_media_shared_with_a_surviving_rom_is_never_removed(tmp_path: Path) -> None:
    """`Game.zip` and `Game.sfc` share a media stem; removing one must not strip the other."""
    roms = tmp_path / "roms"
    snes = roms / "snes"
    (snes / "images").mkdir(parents=True)
    (snes / "Game.sfc").write_bytes(b"same")
    (snes / "Game.zip").write_bytes(b"same")
    (snes / "images" / "Game-image.png").write_bytes(b"shared art")

    with Cache(tmp_path / "c.db") as cache:
        entries = build_entries(scan_tree(roms), cache=cache, writer=BatoceraWriter())
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)
    plan = plan_removals(
        report, entries=entries, action=Action.QUARANTINE, quarantine_dir=tmp_path / "q", writer=BatoceraWriter()
    )
    apply_plan(plan, apply=True, journal_dir=tmp_path / "j", writer=BatoceraWriter())

    assert (snes / "images" / "Game-image.png").read_bytes() == b"shared art"


def test_quarantine_never_overwrites_an_earlier_run(tmp_path: Path) -> None:
    roms, _snes = _library(tmp_path)
    quarantine = tmp_path / "q"
    (quarantine / "snes").mkdir(parents=True)
    (quarantine / "snes" / "Game (USA) [dup].sfc").write_bytes(b"from an earlier run")

    with Cache(tmp_path / "c.db") as cache:
        entries = build_entries(scan_tree(roms), cache=cache, writer=BatoceraWriter())
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)
    plan = plan_removals(
        report, entries=entries, action=Action.QUARANTINE, quarantine_dir=quarantine, writer=BatoceraWriter()
    )
    apply_plan(plan, apply=True, journal_dir=tmp_path / "j", writer=BatoceraWriter())

    assert (quarantine / "snes" / "Game (USA) [dup].sfc").read_bytes() == b"from an earlier run"
    assert (quarantine / "snes" / "Game (USA) [dup]~1.sfc").read_bytes() == b"identical content"


def test_removal_drops_the_gamelist_entry_too(tmp_path: Path) -> None:
    roms, snes = _library(tmp_path)
    (snes / "gamelist.xml").write_text(
        '<?xml version="1.0"?>\n<gameList>\n'
        "  <game><path>./Game (USA).sfc</path><name>Game</name></game>\n"
        "  <game><path>./Game (USA) [dup].sfc</path><name>Game</name></game>\n"
        "</gameList>\n"
    )

    with Cache(tmp_path / "c.db") as cache:
        entries = build_entries(scan_tree(roms), cache=cache, writer=BatoceraWriter())
    report = find_duplicates(entries, keep_priority=KEEP_PRIORITY)
    plan = plan_removals(
        report, entries=entries, action=Action.QUARANTINE, quarantine_dir=tmp_path / "q", writer=BatoceraWriter()
    )
    outcome = apply_plan(plan, apply=True, journal_dir=tmp_path / "j", writer=BatoceraWriter())

    assert outcome.entries_unlisted == 1
    remaining = [entry.rom_path.name for entry in BatoceraWriter().list_entries(snes)]
    assert remaining == ["Game (USA).sfc"]
