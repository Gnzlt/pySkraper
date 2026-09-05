"""Configuration: precedence order, validation that catches real mistakes,
and the file-permission discipline for a file holding two passwords."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from pyskraper.config import Config, ConfigError, load_config, save_config


def _write(path: Path, data: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


def test_device_profile_supplies_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYSKRAPER_CONFIG", raising=False)
    cfg = _write(tmp_path / "config.yaml", {"device": "anbernic-rg35xx-2024"})
    config = load_config(config_path=cfg, use_env=False)
    assert config.images.max_width == 640
    assert config.images.max_height == 480


def test_a_different_device_supplies_its_own_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The profile is read, not assumed -- a 640x480 default would pass the test above."""
    monkeypatch.delenv("PYSKRAPER_CONFIG", raising=False)
    cfg = _write(tmp_path / "config.yaml", {"device": "trimui-brick"})
    config = load_config(config_path=cfg, use_env=False)
    assert (config.images.max_width, config.images.max_height) == (1024, 768)


def test_a_retired_device_profile_is_rejected_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Profiles that no longer exist must fail loudly, not fall back to a default."""
    monkeypatch.delenv("PYSKRAPER_CONFIG", raising=False)
    cfg = _write(tmp_path / "config.yaml", {"device": "generic-batocera"})
    with pytest.raises(KeyError, match="generic-batocera"):
        load_config(config_path=cfg, use_env=False)


def _knulli_card(tmp_path: Path, board: str) -> Path:
    """A card that says which handheld it came out of. Returns its roms folder."""
    volume = tmp_path / "SHARE"
    (volume / "system" / "configs" / "batteryplus").mkdir(parents=True)
    (volume / "system" / "configs" / "batteryplus" / "knulli.board").write_text(f"{board}\n")
    roms = volume / "roms"
    roms.mkdir()
    return roms


def test_auto_reads_the_device_off_the_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYSKRAPER_CONFIG", raising=False)
    roms = _knulli_card(tmp_path, "trimui-brick")
    cfg = _write(tmp_path / "config.yaml", {"device": "auto", "paths": {"roms": str(roms)}})

    config = load_config(config_path=cfg, use_env=False)

    assert config.device == "trimui-brick", "`auto` must not survive into the resolved config"
    assert (config.images.max_width, config.images.max_height) == (1024, 768)


def test_auto_falls_back_when_the_card_says_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`auto` asks for a better default, and a miss is not an error."""
    monkeypatch.delenv("PYSKRAPER_CONFIG", raising=False)
    roms = tmp_path / "SHARE" / "roms"
    roms.mkdir(parents=True)
    cfg = _write(tmp_path / "config.yaml", {"device": "auto", "paths": {"roms": str(roms)}})

    config = load_config(config_path=cfg, use_env=False)

    assert config.device == "anbernic-rg35xx-2024"


def test_auto_without_a_rom_path_still_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no card to read, so there is nothing to detect -- and no crash."""
    monkeypatch.delenv("PYSKRAPER_CONFIG", raising=False)
    cfg = _write(tmp_path / "config.yaml", {"device": "auto"})

    assert load_config(config_path=cfg, use_env=False).device == "anbernic-rg35xx-2024"


def test_auto_can_come_from_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--device auto` has to resolve the same way `device: auto` in a file does."""
    monkeypatch.delenv("PYSKRAPER_CONFIG", raising=False)
    roms = _knulli_card(tmp_path, "powkiddy-x55")
    cfg = _write(tmp_path / "config.yaml", {})

    config = load_config(
        config_path=cfg,
        use_env=False,
        overrides={"device": "auto", "paths": {"roms": roms}},
    )

    assert config.device == "powkiddy-x55"
    assert (config.images.max_width, config.images.max_height) == (1280, 720)


def test_an_explicit_size_still_beats_a_detected_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection feeds the profile layer, which is still the bottom of the chain."""
    monkeypatch.delenv("PYSKRAPER_CONFIG", raising=False)
    roms = _knulli_card(tmp_path, "trimui-brick")
    cfg = _write(
        tmp_path / "config.yaml",
        {"device": "auto", "paths": {"roms": str(roms)}, "images": {"max_width": 320}},
    )

    config = load_config(config_path=cfg, use_env=False)

    assert config.images.max_width == 320
    assert config.images.max_height == 768, "the rest of the detected profile still applies"


def test_explicit_value_beats_the_device_profile(tmp_path: Path) -> None:
    """A profile only ever supplies defaults. If the user wrote it down, they win."""
    cfg = _write(
        tmp_path / "config.yaml",
        {"device": "anbernic-rg35xx-2024", "images": {"max_width": 1280}, "media": {"manual": ["manuel"]}},
    )
    config = load_config(config_path=cfg, use_env=False)
    assert config.images.max_width == 1280
    assert config.media["manual"] == ["manuel"]
    # Untouched profile values survive the merge.
    assert config.images.max_height == 480


def test_env_beats_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write(tmp_path / "config.yaml", {"screenscraper": {"ssid": "from-file"}})
    monkeypatch.setenv("PYSKRAPER_SSID", "from-env")
    config = load_config(config_path=cfg)
    assert config.screenscraper.ssid == "from-env"


def test_cli_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write(tmp_path / "config.yaml", {})
    monkeypatch.setenv("PYSKRAPER_REGION", "jp")
    config = load_config(config_path=cfg, overrides={"preferences": {"region": "eu"}})
    assert config.preferences.region == "eu"


def test_full_precedence_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write(tmp_path / "config.yaml", {"device": "anbernic-rg35xx-2024", "preferences": {"language": "fr"}})
    monkeypatch.setenv("PYSKRAPER_LANGUAGE", "de")
    monkeypatch.setenv("PYSKRAPER_REGION", "jp")

    config = load_config(config_path=cfg, overrides={"preferences": {"language": "es"}})
    assert config.preferences.language == "es", "CLI wins"
    assert config.preferences.region == "jp", "env wins where CLI is silent"
    assert config.images.max_width == 640, "profile fills what nobody else set"


def test_softname_with_a_space_is_rejected(tmp_path: Path) -> None:
    """A space in softname silently corrupts the media URLs the API returns,
    so it has to fail at config load rather than halfway through a run."""
    cfg = _write(tmp_path / "config.yaml", {"screenscraper": {"softname": "Screen Scraper Lite"}})
    with pytest.raises(ConfigError, match="softname"):
        load_config(config_path=cfg, use_env=False)


def test_unknown_key_is_an_error_not_a_silent_no_op(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "config.yaml", {"prefrences": {"region": "us"}})
    with pytest.raises(ConfigError):
        load_config(config_path=cfg, use_env=False)


def test_missing_credentials_are_named(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "config.yaml", {"screenscraper": {"ssid": "me", "sspassword": "pw"}})
    config = load_config(config_path=cfg, use_env=False)
    with pytest.raises(ConfigError) as excinfo:
        config.require_credentials()
    assert "devid" in str(excinfo.value)
    assert "devpassword" in str(excinfo.value)


def test_missing_rom_path_explains_itself(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "config.yaml", {"paths": {"roms": str(tmp_path / "nope")}})
    config = load_config(config_path=cfg, use_env=False)
    with pytest.raises(ConfigError, match="does not exist"):
        config.require_roms()


def test_tilde_is_expanded(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "config.yaml", {"paths": {"cache": "~/somewhere"}})
    config = load_config(config_path=cfg, use_env=False)
    assert "~" not in str(config.paths.cache)


def test_saved_config_is_not_world_readable(tmp_path: Path) -> None:
    """The file holds two passwords. It must never exist, even briefly, with
    permissions that let another account read it."""
    config = Config()
    target = save_config(config, tmp_path / "out" / "config.yaml")
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_saved_config_round_trips(tmp_path: Path) -> None:
    original = load_config(config_path=_write(tmp_path / "in.yaml", {"preferences": {"region": "jp"}}), use_env=False)
    saved = save_config(original, tmp_path / "out.yaml")
    reloaded = load_config(config_path=saved, use_env=False)
    assert reloaded.preferences.region == "jp"
    assert reloaded.media == original.media


def test_save_leaves_no_part_file(tmp_path: Path) -> None:
    target = save_config(Config(), tmp_path / "config.yaml")
    assert not target.with_name(target.name + ".part").exists()
