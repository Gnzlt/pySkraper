"""The `dedupe` and `verify` commands, exercised through the CLI.

Everything here is about the refusals. The detection logic is tested in
test_dedupe.py; what these tests pin down is that the command cannot be talked
into destroying something by accident -- no --apply means no change, and
--delete --non-interactive is refused outright with no override.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pyskraper.cli import app

runner = CliRunner()


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A card with one exact duplicate pair, and an isolated config/cache."""
    roms = tmp_path / "roms"
    snes = roms / "snes"
    snes.mkdir(parents=True)
    (snes / "Game (USA).sfc").write_bytes(b"identical content")
    (snes / "Game (USA) [dup].sfc").write_bytes(b"identical content")

    config = tmp_path / "config.yaml"
    config.write_text(
        "screenscraper:\n"
        "  devid: x\n"
        "  devpassword: y\n"
        f"paths:\n  roms: {roms}\n  cache: {tmp_path / 'cache'}\n"
        f"dedupe:\n  quarantine_dir: {tmp_path / 'quarantine'}\n"
    )
    monkeypatch.setenv("PYSKRAPER_CONFIG", str(config))
    for name in ("PYSKRAPER_ROMS", "PYSKRAPER_CACHE", "PYSKRAPER_DEVID", "PYSKRAPER_DEVPASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return roms


def test_delete_with_non_interactive_is_refused(library: Path) -> None:
    result = runner.invoke(app, ["dedupe", "--apply", "--delete", "--non-interactive"])

    assert result.exit_code == 2
    assert "refused" in result.output.lower()
    # And it refused before doing anything at all.
    assert (library / "snes" / "Game (USA) [dup].sfc").exists()


def test_delete_with_global_non_interactive_is_refused(library: Path) -> None:
    """The flag before the subcommand counts too.

    It used to be read by the callback and dropped, so this spelling walked
    straight past the refusal the spelling above hits.
    """
    result = runner.invoke(app, ["--non-interactive", "dedupe", "--apply", "--delete"])

    assert result.exit_code == 2
    assert "refused" in result.output.lower()
    assert (library / "snes" / "Game (USA) [dup].sfc").exists()


def test_reports_without_apply_and_changes_nothing(library: Path) -> None:
    result = runner.invoke(app, ["dedupe"])

    assert result.exit_code == 0
    assert "Nothing was changed" in result.output
    assert (library / "snes" / "Game (USA) [dup].sfc").exists()
    assert (library / "snes" / "Game (USA).sfc").exists()


def test_apply_quarantines_the_duplicate(library: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["dedupe", "--apply"])

    assert result.exit_code == 0
    assert not (library / "snes" / "Game (USA) [dup].sfc").exists()
    assert (library / "snes" / "Game (USA).sfc").exists()
    assert (tmp_path / "quarantine" / "snes" / "Game (USA) [dup].sfc").exists()


def test_delete_aborts_unless_the_word_is_typed(library: Path) -> None:
    result = runner.invoke(app, ["dedupe", "--apply", "--delete"], input="yes\n")

    assert "Cancelled" in result.output
    assert (library / "snes" / "Game (USA) [dup].sfc").exists()


def test_delete_proceeds_only_on_the_exact_confirmation(library: Path) -> None:
    result = runner.invoke(app, ["dedupe", "--apply", "--delete"], input="delete\n")

    assert result.exit_code == 0
    assert not (library / "snes" / "Game (USA) [dup].sfc").exists()
    assert (library / "snes" / "Game (USA).sfc").exists()


def test_verify_reports_without_changing_anything(library: Path) -> None:
    (library / "snes" / "images").mkdir()
    orphan = library / "snes" / "images" / "Ghost-image.png"
    orphan.write_bytes(b"art")

    result = runner.invoke(app, ["verify", "--clean-orphans"])

    assert "Would remove" in result.output
    assert orphan.exists()


def test_verify_clean_orphans_needs_apply(library: Path) -> None:
    (library / "snes" / "images").mkdir()
    orphan = library / "snes" / "images" / "Ghost-image.png"
    orphan.write_bytes(b"art")

    runner.invoke(app, ["verify", "--clean-orphans", "--apply"])

    assert not orphan.exists()
    # ROMs are never in scope for --clean-orphans.
    assert (library / "snes" / "Game (USA).sfc").exists()
