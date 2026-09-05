"""The guided setup.

Two properties matter more than any individual prompt, and both are failures
that would only show up in someone else's hands:

* abandoning the wizard leaves nothing written -- no half-configured file, no
  cache directory, nothing;
* no prompt ever appears when there is nobody to answer it, because a wizard
  that blocks inside a cron job hangs forever and says nothing about why.

Everything here drives the wizard through piped stdin. Credential verification
is stubbed, so no test touches the network.
"""

from __future__ import annotations

from collections.abc import Coroutine, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pyskraper import paths, wizard
from pyskraper.api.errors import AuthError
from pyskraper.api.quota import QuotaSnapshot
from pyskraper.cli import app
from pyskraper.config import RECOMMENDED_MEDIA
from pyskraper.devices import by_vendor

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every run gets its own base directory and a clean environment."""
    base = tmp_path / "pySkraper"
    base.mkdir()
    paths.set_base_dir(base)
    # Belt and braces: the env var survives even if something calls
    # set_base_dir(None), so a bug in the CLI cannot let a test escape into the
    # real project folder -- which is exactly what it did the first time.
    monkeypatch.setenv("PYSKRAPER_HOME", str(base))
    for name in (
        "PYSKRAPER_CONFIG",
        "PYSKRAPER_ROMS",
        "PYSKRAPER_DEVID",
        "PYSKRAPER_DEVPASSWORD",
        "PYSKRAPER_SSID",
        "PYSKRAPER_SSPASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    # CliRunner always pipes stdin, so can_prompt() would be False everywhere and
    # every test would hit the "needs a terminal" refusal. The two tests that
    # exercise that refusal switch it back off explicitly.
    monkeypatch.setattr("pyskraper.cli.can_prompt", lambda: True)
    # No real SD card. Without this the wizard finds whatever is plugged into
    # the machine running the tests, and every answer after the card prompt
    # lands on the wrong question.
    monkeypatch.setattr(wizard, "mount_roots", list)
    yield base
    paths.set_base_dir(None)


@pytest.fixture
def card(tmp_path: Path) -> Path:
    """A small but realistic ROM tree: two known systems, one unrecognised."""
    roms = tmp_path / "SHARE" / "roms"
    for system, count in (("snes", 3), ("gb", 2)):
        folder = roms / system
        folder.mkdir(parents=True)
        suffix = ".sfc" if system == "snes" else ".gb"
        for index in range(count):
            (folder / f"Game {index}{suffix}").write_bytes(b"rom" + bytes([index]))
    (roms / "not-a-console").mkdir()
    return roms


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch) -> QuotaSnapshot:
    """Credential checks succeed without a network call."""
    snapshot = QuotaSnapshot(
        max_threads=2,
        max_requests_per_day=20_000,
        requests_today=588,
        max_requests_ko_per_day=200,
        username="tester",
    )
    monkeypatch.setattr(wizard, "_verify", lambda config: snapshot)
    return snapshot


def _answers(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


# The happy path, answered from the top: developer credentials, no member
# account, type the card path, default device brand *and* model, keep all
# systems, recommended media, default language and region, no estimate, then
# save and exit.  Step 3 asks twice, so it takes two of these.
HAPPY = ["dev-id", "dev-pass", "n", "{roms}", "", "", "", "", "", "", "s"]


def _script(roms: Path, answers: list[str] | None = None) -> str:
    return _answers(*[a.format(roms=roms) for a in (answers or HAPPY)])


# "Other / custom resolution" sits after the last vendor, so adding a device
# should not silently retarget these scripts at a real handheld.
_OTHER_BRAND = str(len(by_vendor()) + 1)


# --------------------------------------------------------------------------
# Nothing is written until the very end
# --------------------------------------------------------------------------


def test_completing_the_wizard_writes_the_config(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    result = runner.invoke(app, ["setup"], input=_script(card))

    assert result.exit_code == 0, result.output
    config_file = _isolated / "pyskraper.yaml"
    assert config_file.exists()
    assert "dev-id" in config_file.read_text()


def test_the_config_is_written_inside_the_base_directory(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    """The portable promise: state lands in the folder, not in the system."""
    runner.invoke(app, ["setup"], input=_script(card))

    assert (_isolated / "pyskraper.yaml").exists()


def test_the_config_is_not_world_readable(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    """It holds two passwords."""
    runner.invoke(app, ["setup"], input=_script(card))

    mode = (_isolated / "pyskraper.yaml").stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_quitting_at_the_end_writes_nothing(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    answers = [*HAPPY[:-1], "q"]

    result = runner.invoke(app, ["setup"], input=_script(card, answers))

    assert result.exit_code == 0
    assert not (_isolated / "pyskraper.yaml").exists()
    assert "Nothing was saved" in result.output


def test_ctrl_c_partway_through_writes_nothing(
    card: Path, verified: QuotaSnapshot, _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(config: object, roms: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard, "step_systems", interrupt)

    result = runner.invoke(app, ["setup"], input=_script(card))

    assert result.exit_code == 130
    assert not (_isolated / "pyskraper.yaml").exists()
    assert "Nothing was saved" in result.output


def test_input_running_out_writes_nothing(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    """A script that answers three questions and then stops."""
    result = runner.invoke(app, ["setup"], input=_answers("dev-id", "dev-pass", "n"))

    assert result.exit_code == 130
    assert not (_isolated / "pyskraper.yaml").exists()


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_bad_credentials_are_re_prompted_rather_than_fatal(
    card: Path, _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second attempt should be accepted, and the run should continue."""
    attempts: list[str] = []

    def flaky(config: object) -> QuotaSnapshot | None:
        devid = config.screenscraper.devid  # type: ignore[attr-defined]
        attempts.append(devid)
        if devid == "wrong":
            return None
        return QuotaSnapshot(max_threads=1)

    monkeypatch.setattr(wizard, "_verify", flaky)
    answers = ["wrong", "nope", "n", "dev-id", "dev-pass", "n", "{roms}", "", "", "", "", "", "", "s"]

    result = runner.invoke(app, ["setup"], input=_script(card, answers))

    assert attempts == ["wrong", "dev-id"]
    assert result.exit_code == 0
    assert (_isolated / "pyskraper.yaml").exists()


def test_an_auth_error_explains_itself_without_leaking_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pyskraper.config import Config

    config = Config()
    config.screenscraper.devid = "dev-id"
    config.screenscraper.devpassword = "s3cr3t-password"

    def reject(coro: Coroutine[object, object, object]) -> None:
        coro.close()  # we are standing in for asyncio.run; nobody else will
        raise AuthError("bad login")

    monkeypatch.setattr("pyskraper.wizard.asyncio.run", reject)

    assert wizard._verify(config) is None
    assert "s3cr3t-password" not in capsys.readouterr().out


def test_anonymous_mode_is_accepted(card: Path, _isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Developer credentials alone are a supported setup, not a failure."""
    monkeypatch.setattr(wizard, "_verify", lambda config: QuotaSnapshot(max_threads=1))

    result = runner.invoke(app, ["setup"], input=_script(card))

    assert result.exit_code == 0
    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "ssid: ''" in saved


# --------------------------------------------------------------------------
# Finding the card
# --------------------------------------------------------------------------


def test_a_typed_path_is_accepted_when_nothing_is_found(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    result = runner.invoke(app, ["setup"], input=_script(card))

    assert result.exit_code == 0
    assert str(card) in (_isolated / "pyskraper.yaml").read_text()


def test_a_bad_path_is_re_prompted(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    answers = ["dev-id", "dev-pass", "n", "/no/such/place", "{roms}", "", "", "", "", "", "", "s"]

    result = runner.invoke(app, ["setup"], input=_script(card, answers))

    assert "Not a directory" in result.output
    assert result.exit_code == 0


def test_the_volume_with_more_recognised_systems_is_offered_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volumes = tmp_path / "Volumes"
    (volumes / "KNULLI").mkdir(parents=True)
    for system in ("snes", "gb", "megadrive"):
        (volumes / "SHARE" / "roms" / system).mkdir(parents=True)
    (volumes / "PLAIN" / "roms" / "snes").mkdir(parents=True)

    monkeypatch.setattr(wizard, "mount_roots", lambda: [volumes])

    found = wizard.find_cards()

    assert [c.volume.name for c in found] == ["SHARE", "PLAIN"]


def test_a_boot_partition_is_reported_but_does_not_decide_the_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It marks every sibling volume equally, so it cannot break a tie.

    Worth pinning down: attaching it to the ranking looks obviously right and
    silently does nothing, because volumes on one mount root are all siblings
    of each other.
    """
    volumes = tmp_path / "Volumes"
    (volumes / "KNULLI").mkdir(parents=True)
    for volume in ("PLAIN", "SHARE"):
        for system in ("snes", "gb"):
            (volumes / volume / "roms" / system).mkdir(parents=True)

    monkeypatch.setattr(wizard, "mount_roots", lambda: [volumes])

    found = wizard.find_cards()

    assert len(found) == 2
    assert all(c.looks_like_a_handheld_card for c in found)
    assert all(c.boot_partition is not None and c.boot_partition.name == "KNULLI" for c in found)


def test_find_cards_ignores_a_volume_with_no_roms_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    volumes = tmp_path / "Volumes"
    (volumes / "Backup" / "documents").mkdir(parents=True)
    monkeypatch.setattr(wizard, "mount_roots", lambda: [volumes])

    assert wizard.find_cards() == []


def test_find_cards_counts_recognised_systems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    volumes = tmp_path / "Volumes"
    roms = volumes / "SHARE" / "roms"
    for folder in ("snes", "gb", "definitely-not-a-console"):
        (roms / folder).mkdir(parents=True)
    monkeypatch.setattr(wizard, "mount_roots", lambda: [volumes])

    found = wizard.find_cards()

    assert found[0].known_systems == 2
    assert found[0].total_systems == 3


# --------------------------------------------------------------------------
# Systems and media
# --------------------------------------------------------------------------


def test_systems_default_to_everything_playable(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    runner.invoke(app, ["setup"], input=_script(card))

    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "- gb" in saved
    assert "- snes" in saved


def test_a_system_can_be_toggled_off(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    """Systems are listed alphabetically, so 1 is gb."""
    answers = ["dev-id", "dev-pass", "n", "{roms}", "", "", "1", "", "", "", "", "s"]

    runner.invoke(app, ["setup"], input=_script(card, answers))

    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "- snes" in saved
    assert "- gb" not in saved


def test_choosing_everything_enables_more_media_than_the_device_default(
    card: Path, verified: QuotaSnapshot, _isolated: Path
) -> None:
    recommended = ["dev-id", "dev-pass", "n", "{roms}", "", "", "", "1", "", "", "s"]
    everything = ["dev-id", "dev-pass", "n", "{roms}", "", "", "", "2", "", "", "s"]

    runner.invoke(app, ["setup"], input=_script(card, recommended))
    first = (_isolated / "pyskraper.yaml").read_text().count("- ")
    (_isolated / "pyskraper.yaml").unlink()

    runner.invoke(app, ["setup"], input=_script(card, everything))
    second = (_isolated / "pyskraper.yaml").read_text().count("- ")

    assert second > first


def test_recommended_media_falls_back_to_the_shared_default_without_a_profile_override(
    card: Path, verified: QuotaSnapshot, _isolated: Path
) -> None:
    """No profile carries media of its own -- "Recommended" must still mean something."""
    answers = ["dev-id", "dev-pass", "n", "{roms}", _OTHER_BRAND, "", "", "1", "", "", "s"]

    runner.invoke(app, ["setup"], input=_script(card, answers))

    saved = (_isolated / "pyskraper.yaml").read_text()
    for tag, keys in RECOMMENDED_MEDIA.items():
        if keys:
            assert f"{tag}:" in saved, f"expected {tag} enabled, not in:\n{saved}"
    assert "video:" not in saved or "video: []" in saved


# --------------------------------------------------------------------------
# Device
# --------------------------------------------------------------------------


def test_the_default_device_supplies_its_screen_size(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    runner.invoke(app, ["setup"], input=_script(card))

    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "device: anbernic-rg35xx-2024" in saved
    assert "max_width: 640" in saved
    assert "max_height: 480" in saved


def test_picking_a_model_uses_that_models_screen(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    """TrimUI is brand 2, Brick is its first model -- a 1024x768 panel."""
    answers = ["dev-id", "dev-pass", "n", "{roms}", "2", "1", "", "", "", "", "s"]

    runner.invoke(app, ["setup"], input=_script(card, answers))

    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "device: trimui-brick" in saved
    assert "max_width: 1024" in saved
    assert "max_height: 768" in saved


def test_a_custom_resolution_is_accepted_for_an_unlisted_device(
    card: Path, verified: QuotaSnapshot, _isolated: Path
) -> None:
    answers = ["dev-id", "dev-pass", "n", "{roms}", _OTHER_BRAND, "800x600", "", "", "", "", "s"]

    runner.invoke(app, ["setup"], input=_script(card, answers))

    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "device: none" in saved
    assert "max_width: 800" in saved
    assert "max_height: 600" in saved


def test_a_malformed_resolution_is_re_prompted_rather_than_fatal(
    card: Path, verified: QuotaSnapshot, _isolated: Path
) -> None:
    answers = ["dev-id", "dev-pass", "n", "{roms}", _OTHER_BRAND, "nonsense", "800x600", "", "", "", "", "s"]

    result = runner.invoke(app, ["setup"], input=_script(card, answers))

    assert result.exit_code == 0, result.output
    assert "max_width: 800" in (_isolated / "pyskraper.yaml").read_text()


def test_an_empty_resolution_leaves_artwork_full_size(card: Path, verified: QuotaSnapshot, _isolated: Path) -> None:
    answers = ["dev-id", "dev-pass", "n", "{roms}", _OTHER_BRAND, "", "", "", "", "", "s"]

    runner.invoke(app, ["setup"], input=_script(card, answers))

    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "device: none" in saved
    assert "max_width:" not in saved or "max_width: null" in saved


@pytest.fixture
def knulli_card(card: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The card fixture, plus what KNULLI leaves behind saying which device it is.

    Mounted the way a real reader presents it: a userdata volume holding
    ``roms/`` and ``system/``, with the boot partition beside it.
    """
    board = card.parent / "system" / "configs" / "batteryplus" / "knulli.board"
    board.parent.mkdir(parents=True)
    board.write_text("trimui-brick\n")
    (tmp_path / "KNULLI").mkdir()
    # Overrides the blanket stub in `_isolated`: this test wants the card found.
    monkeypatch.setattr(wizard, "mount_roots", lambda: [tmp_path])
    return card


# Card found rather than typed, then the detected device accepted -- which is
# why this is two answers shorter than HAPPY: step 3 asks once, not twice.
DETECTED = ["dev-id", "dev-pass", "n", "1", "", "", "", "", "", "s"]


def test_a_detected_device_is_offered_and_skips_both_lists(
    knulli_card: Path, verified: QuotaSnapshot, _isolated: Path
) -> None:
    result = runner.invoke(app, ["setup"], input=_script(knulli_card, DETECTED))

    assert result.exit_code == 0, result.output
    assert "Detected from the card" in result.output
    assert "TrimUI Brick" in result.output
    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "device: trimui-brick" in saved
    assert "max_width: 1024" in saved
    assert "max_height: 768" in saved


def test_declining_the_detection_falls_through_with_it_preselected(
    knulli_card: Path, verified: QuotaSnapshot, _isolated: Path
) -> None:
    """ "No, but close" must cost two keystrokes, not a restart of the step.

    Answering "n" then Enter twice has to land back on the detected device, so
    the brand and model prompts have to be defaulted at it rather than at the
    built-in default profile.
    """
    answers = ["dev-id", "dev-pass", "n", "1", "n", "", "", "", "", "", "", "s"]

    result = runner.invoke(app, ["setup"], input=_script(knulli_card, answers))

    assert result.exit_code == 0, result.output
    saved = (_isolated / "pyskraper.yaml").read_text()
    assert "device: trimui-brick" in saved, "the lists should default to what was detected"


def test_declining_still_lets_another_device_be_chosen(
    knulli_card: Path, verified: QuotaSnapshot, _isolated: Path
) -> None:
    """The fall-through is the real list, not a two-option confirmation."""
    answers = ["dev-id", "dev-pass", "n", "1", "n", "1", "1", "", "", "", "", "s"]

    result = runner.invoke(app, ["setup"], input=_script(knulli_card, answers))

    assert result.exit_code == 0, result.output
    assert "device: anbernic-rg35xx-2024" in (_isolated / "pyskraper.yaml").read_text()


def test_a_shared_board_names_the_model_it_could_not_rule_out(
    card: Path, tmp_path: Path, verified: QuotaSnapshot, _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One KNULLI image covers the RG35XX 2024 and the Plus, so say so."""
    board = card.parent / "system" / "configs" / "batteryplus" / "knulli.board"
    board.parent.mkdir(parents=True)
    board.write_text("rg35xx-plus\n")
    monkeypatch.setattr(wizard, "mount_roots", lambda: [tmp_path])

    result = runner.invoke(app, ["setup"], input=_script(card, DETECTED))

    assert result.exit_code == 0, result.output
    assert "Same KNULLI image as" in result.output
    assert "RG35XX Plus" in result.output


def test_an_unrecognised_board_leaves_the_step_as_it_was(
    card: Path, tmp_path: Path, verified: QuotaSnapshot, _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device newer than the roster must not break the wizard for it."""
    board = card.parent / "system" / "configs" / "batteryplus" / "knulli.board"
    board.parent.mkdir(parents=True)
    board.write_text("rg99xx-quantum\n")
    monkeypatch.setattr(wizard, "mount_roots", lambda: [tmp_path])
    # Two answers for step 3 again, because it fell back to brand-then-model.
    answers = ["dev-id", "dev-pass", "n", "1", "", "", "", "", "", "", "s"]

    result = runner.invoke(app, ["setup"], input=_script(card, answers))

    assert result.exit_code == 0, result.output
    assert "Detected from the card" not in result.output
    assert "device: anbernic-rg35xx-2024" in (_isolated / "pyskraper.yaml").read_text()


# --------------------------------------------------------------------------
# Never prompt when nobody is there
# --------------------------------------------------------------------------


def test_bare_command_without_a_terminal_prints_commands_instead_of_prompting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def should_not_run(*args: object, **kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr("pyskraper.cli.can_prompt", lambda: False)
    monkeypatch.setattr("pyskraper.cli.run_wizard", should_not_run)

    result = runner.invoke(app, [])

    assert not called
    assert "pyskraper setup" in result.output
    assert result.exit_code == 0


def test_non_interactive_never_starts_the_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyskraper.cli.can_prompt", lambda: True)
    called = False

    def should_not_run(*args: object, **kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr("pyskraper.cli.run_wizard", should_not_run)

    result = runner.invoke(app, ["--non-interactive"])

    assert not called
    assert result.exit_code == 0


def test_setup_refuses_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyskraper.cli.can_prompt", lambda: False)

    result = runner.invoke(app, ["setup"], input="")

    assert result.exit_code == 2
    assert "needs a terminal" in result.output


# --------------------------------------------------------------------------
# The portable promise
# --------------------------------------------------------------------------


def test_a_full_run_writes_nothing_outside_the_base_directory(
    card: Path, verified: QuotaSnapshot, _isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarantee the whole layout exists for, checked rather than assumed.

    $HOME is pointed at an empty directory for the duration: if any code path
    still reaches for ~/.config or ~/Library, it lands here and the directory
    stops being empty.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    result = runner.invoke(app, ["setup"], input=_script(card))

    assert result.exit_code == 0
    assert list(fake_home.iterdir()) == [], "something was written outside the base directory"
    assert (_isolated / "pyskraper.yaml").exists()
