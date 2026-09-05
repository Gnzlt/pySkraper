"""The resume journal. A full library at one thread runs for hours, so being
interrupted is routine and must cost nothing."""

from __future__ import annotations

from pathlib import Path

from pyskraper.core.journal import RunJournal, journal_path_for


def test_records_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    with RunJournal(path) as journal:
        journal.record(Path("/roms/snes/A.sfc"), "snes", "hash", matched=True)
        journal.record(Path("/roms/snes/B.sfc"), "snes", "filename", matched=True)

    reloaded = RunJournal(path)
    done = reloaded.load()
    assert done == {"/roms/snes/A.sfc", "/roms/snes/B.sfc"}
    assert Path("/roms/snes/A.sfc") in reloaded
    assert Path("/roms/snes/C.sfc") not in reloaded


def test_unmatched_roms_are_journalled_too(tmp_path: Path) -> None:
    """An unmatched ROM already spent KO quota. Retrying it on resume would
    spend it again to reach the same answer."""
    path = tmp_path / "run.jsonl"
    with RunJournal(path) as journal:
        journal.record(Path("/roms/snes/weird.sfc"), "snes", "unmatched", matched=False)
    reloaded = RunJournal(path)
    reloaded.load()
    assert Path("/roms/snes/weird.sfc") in reloaded


def test_torn_final_line_is_survivable(tmp_path: Path) -> None:
    """A run killed mid-write leaves a half-written last line. Losing that one
    entry means re-doing one ROM; refusing to load would mean re-doing all."""
    path = tmp_path / "run.jsonl"
    with RunJournal(path) as journal:
        journal.record(Path("/roms/snes/A.sfc"), "snes", "hash", matched=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"path": "/roms/snes/B.sfc", "sys')

    assert RunJournal(path).load() == {"/roms/snes/A.sfc"}


def test_entries_are_flushed_immediately(tmp_path: Path) -> None:
    """Buffered writes would lose the last minutes of a long run on a kill."""
    path = tmp_path / "run.jsonl"
    journal = RunJournal(path)
    journal.open()
    journal.record(Path("/roms/snes/A.sfc"), "snes", "hash", matched=True)
    assert RunJournal(path).load() == {"/roms/snes/A.sfc"}
    journal.close()


def test_missing_journal_loads_empty(tmp_path: Path) -> None:
    assert RunJournal(tmp_path / "nope.jsonl").load() == set()


def test_clear_resets(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    with RunJournal(path) as journal:
        journal.record(Path("/roms/snes/A.sfc"), "snes", "hash", matched=True)
    journal.clear()
    assert not path.exists()
    assert RunJournal(path).load() == set()


def test_separate_libraries_get_separate_journals(tmp_path: Path) -> None:
    """Two cards must not share resume state, or scraping the second would skip
    everything the first already did."""
    a = journal_path_for(tmp_path, Path("/Volumes/CARD_A/roms"))
    b = journal_path_for(tmp_path, Path("/Volumes/CARD_B/roms"))
    assert a != b
    assert journal_path_for(tmp_path, Path("/Volumes/CARD_A/roms")) == a
