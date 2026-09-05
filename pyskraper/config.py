"""Configuration models and the resolution order that assembles them.

Precedence, highest first::

    CLI flag  ->  PYSKRAPER_* env var  ->  config file  ->  device profile  ->  built-in default

The device profile sits *below* the config file deliberately: it seeds sensible
values for the hardware, but anything the user wrote down themselves wins.

``device`` itself may be the literal ``auto``, from any of the three upper
layers, which means "read the board name off the card".  That is resolved here,
before the profile contributes anything, so the rest of the chain is unchanged
and a saved config records the concrete profile rather than the request.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .core.atomic import atomic_text
from .devices import DEFAULT_PROFILE, profile_defaults
from .logging_setup import register_secret
from .paths import CONFIG_FILENAME, boot_partition_beside, cache_dir, config_path

__all__ = [
    "AUTO_DEVICE",
    "Config",
    "ConfigError",
    "default_config_path",
    "find_config_file",
    "load_config",
    "save_config",
]

# `device: auto` asks for the profile to be read off the card instead of named.
# It is resolved during :func:`load_config`, so a `Config` never holds it and a
# saved config records whichever concrete profile the card produced.
AUTO_DEVICE = "auto"


log = logging.getLogger(__name__)


class ConfigError(Exception):
    """A configuration problem the user can act on."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(_Model):
    devid: str = ""
    devpassword: str = ""
    ssid: str = ""
    sspassword: str = ""
    softname: str = "pySkraper"

    @field_validator("softname")
    @classmethod
    def _no_spaces(cls, value: str) -> str:
        # A space in softname silently corrupts the media URLs the API returns,
        # so it is rejected here rather than debugged later.
        if not value.strip():
            raise ValueError("softname must not be empty")
        if any(ch.isspace() for ch in value):
            raise ValueError(f"softname must not contain spaces (got {value!r})")
        return value

    def missing(self) -> list[str]:
        """Only the developer pair is mandatory.

        Verified against the live API: ``jeuInfos``, ``systemesListe`` and media
        downloads all work with developer credentials alone. A member account
        raises the allowance substantially (more threads, faster downloads), but
        it is an upgrade, not a requirement -- so refusing to run without one
        would lock people out of a working anonymous mode.
        """
        return [f for f in ("devid", "devpassword") if not getattr(self, f).strip()]

    @property
    def has_user_account(self) -> bool:
        return bool(self.ssid.strip() and self.sspassword.strip())

    def is_complete(self) -> bool:
        return not self.missing()


def _expand(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    return Path(str(value)).expanduser()


class Paths(_Model):
    roms: Path | None = None
    cache: Path = Field(default_factory=cache_dir)
    """Resolved against :func:`paths.base_dir` rather than a system cache
    location, so the database travels with the folder it belongs to."""

    @field_validator("roms", "cache", mode="before")
    @classmethod
    def _expanduser(cls, value: Any) -> Any:
        return _expand(value) if value is not None else None


class SystemsConfig(_Model):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class Preferences(_Model):
    language: str = "en"
    region: str = "us"
    region_fallback: list[str] = Field(default_factory=lambda: ["wor", "us", "eu", "jp", "ss", "cus"])


class Identification(_Model):
    use_hash: bool = True
    """--no-hash sets this false. Documented as strictly worse: it gives up
    content matching and leans on filenames, which are frequently wrong."""
    max_hash_size: int = 0
    hash_archives: Literal["auto", "archive", "contents", "both"] = "auto"
    use_serial: bool = True
    filename_fallback: bool = True
    search_fallback: bool = True


class OutputConfig(_Model):
    # One value, because one writer exists.  Adding a format means adding a
    # `Writer` in `pyskraper/output/` and naming it here, in that order --
    # a name with nothing behind it only fails once someone tries to scrape.
    format: Literal["batocera"] = "batocera"
    overwrite: bool = False
    merge_gamelist: bool = True
    write_hashes: bool = True
    write_scraper_id: bool = True


class ImagesConfig(_Model):
    max_width: int | None = None
    max_height: int | None = None
    convert_to: Literal["png", "jpg", "webp"] | None = None
    prefer_server_resize: bool = True


class DedupeConfig(_Model):
    detect: list[str] = Field(default_factory=lambda: ["exact", "same-game"])
    action: Literal["delete", "report-only"] = "delete"
    keep_priority: list[str] = Field(
        default_factory=lambda: ["region:us", "verified", "latest-revision", "in-gamelist", "shortest-name"]
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_quarantine(cls, value: Any) -> Any:
        """Accept a config written before quarantine was removed.

        `extra="forbid"` is right for typos and wrong for a setting this tool
        itself told users to write, so the two dead keys are dropped rather than
        rejected.  `action: quarantine` becomes `delete` because that is what it
        meant -- "act on duplicates when I ask" -- and acting still costs an
        `--apply` and a typed confirmation, so nothing goes without being asked.
        """
        if not isinstance(value, dict) or not ({"quarantine_dir", "action"} & value.keys()):
            return value
        value = dict(value)
        if value.pop("quarantine_dir", None) is not None:
            log.warning("dedupe.quarantine_dir is no longer used and was ignored; duplicates are deleted now.")
        if value.get("action") == "quarantine":
            value["action"] = "delete"
            log.warning(
                "dedupe.action: quarantine is gone — reading it as `delete`. "
                "`dedupe --apply` deletes after you type `delete` to confirm; "
                "set it to `report-only` if you would rather it never act."
            )
        return value


class NetworkConfig(_Model):
    threads: int | Literal["auto"] = "auto"
    retries: int = 4
    timeout: float = 30.0
    stop_at_quota_pct: float = 95.0

    @property
    def jobs_cap(self) -> int | None:
        """``--jobs``-style ceiling, or ``None`` to accept whatever the API grants."""
        return None if self.threads == "auto" else int(self.threads)


DEFAULT_MEDIA: dict[str, list[str]] = {
    "image": ["ss", "sstitle", "box-2D"],
    "thumbnail": ["box-2D", "box-3D"],
    "marquee": ["wheel", "wheel-hd", "screenmarqueesmall", "screenmarquee"],
    "titleshot": ["sstitle"],
    "fanart": ["fanart"],
    "boxart": ["box-2D"],
    "boxback": ["box-2D-back"],
    "wheel": ["wheel", "wheel-hd"],
    "cartridge": ["support-2D"],
    "mix": ["mixrbv2", "mixrbv1"],
    "map": ["maps"],
    "bezel": [],
    "video": ["video-normalized", "video"],
    "manual": ["manuel"],
    "magazine": ["magazine"],
}

# The shared default: the four types Skyscraper itself ships as its default
# artwork set (cover, screenshot, wheel, marquee), plus thumbnail and titleshot
# since ES/Batocera themes commonly use them too. Everything else -- video,
# manual, magazine, map, fanart, mix, boxback, cartridge -- is large and niche
# enough that every scraper checked treats it as opt-in, not a default.
_RECOMMENDED_TAGS = ("image", "thumbnail", "marquee", "titleshot", "boxart", "wheel")
RECOMMENDED_MEDIA: dict[str, list[str]] = {
    tag: (list(keys) if tag in _RECOMMENDED_TAGS else []) for tag, keys in DEFAULT_MEDIA.items()
}


class Config(_Model):
    screenscraper: Credentials = Field(default_factory=Credentials)
    device: str | None = DEFAULT_PROFILE
    paths: Paths = Field(default_factory=Paths)
    systems: SystemsConfig = Field(default_factory=SystemsConfig)
    preferences: Preferences = Field(default_factory=Preferences)
    identification: Identification = Field(default_factory=Identification)
    output: OutputConfig = Field(default_factory=OutputConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    media: dict[str, list[str]] = Field(default_factory=lambda: copy.deepcopy(RECOMMENDED_MEDIA))
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    def register_secrets(self) -> None:
        register_secret(self.screenscraper.devpassword)
        register_secret(self.screenscraper.sspassword)

    def require_credentials(self) -> None:
        missing = self.screenscraper.missing()
        if missing:
            raise ConfigError(
                "Missing ScreenScraper developer credentials: "
                + ", ".join(missing)
                + ".\nSet them in your config file, or export "
                + ", ".join(f"PYSKRAPER_{m.upper()}" for m in missing)
                + ".\nRun `pyskraper` with no arguments for the guided setup."
            )

    def require_roms(self) -> Path:
        roms = self.paths.roms
        if roms is None:
            raise ConfigError("No ROM directory configured. Set paths.roms, or pass --roms /Volumes/<card>/roms.")
        if not roms.exists():
            raise ConfigError(f"ROM directory does not exist: {roms}")
        if not roms.is_dir():
            raise ConfigError(f"ROM path is not a directory: {roms}")
        return roms

    def enabled_tags(self) -> list[str]:
        return [tag for tag, keys in self.media.items() if keys]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

# Only non-secret values are worth putting in env vars for convenience, but the
# passwords belong here too: an env var keeps them out of a file on a shared card.
_ENV_MAP: dict[str, tuple[str, ...]] = {
    "PYSKRAPER_DEVID": ("screenscraper", "devid"),
    "PYSKRAPER_DEVPASSWORD": ("screenscraper", "devpassword"),
    "PYSKRAPER_SSID": ("screenscraper", "ssid"),
    "PYSKRAPER_SSPASSWORD": ("screenscraper", "sspassword"),
    "PYSKRAPER_SOFTNAME": ("screenscraper", "softname"),
    "PYSKRAPER_DEVICE": ("device",),
    "PYSKRAPER_ROMS": ("paths", "roms"),
    "PYSKRAPER_CACHE": ("paths", "cache"),
    "PYSKRAPER_LANGUAGE": ("preferences", "language"),
    "PYSKRAPER_REGION": ("preferences", "region"),
    "PYSKRAPER_OUTPUT": ("output", "format"),
    "PYSKRAPER_JOBS": ("network", "threads"),
}

_CONFIG_FILENAME = CONFIG_FILENAME


def default_config_path() -> Path:
    return config_path()


def find_config_file(explicit: Path | None = None) -> Path | None:
    """Search path: ``--config`` -> ``$PYSKRAPER_CONFIG`` -> base dir -> cwd."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return path

    env_path = os.environ.get("PYSKRAPER_CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.exists():
            raise ConfigError(f"PYSKRAPER_CONFIG points at a missing file: {path}")
        return path

    for candidate in (default_config_path(), Path.cwd() / _CONFIG_FILENAME):
        if candidate.exists():
            return candidate
    return None


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` onto ``base``, recursing into nested dicts only."""
    result = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _set_path(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = target
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def _env_layer() -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for env_name, path in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        value: Any = raw
        if env_name == "PYSKRAPER_JOBS":
            value = raw if raw == "auto" else int(raw)
        _set_path(layer, path, value)
    return layer


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return raw


def _highest(layers: tuple[dict[str, Any], ...], *keys: str) -> Any:
    """The value at ``keys`` from the highest-precedence layer that has one."""
    found: Any = None
    for layer in layers:
        cursor: Any = layer
        for key in keys:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        if cursor is not None:
            found = cursor
    return found


def _detect_device(layers: tuple[dict[str, Any], ...]) -> str:
    """Resolve ``device: auto`` by reading the board name off the card.

    Reads ``paths.roms`` straight out of the layers rather than waiting for the
    merged config, because the merge needs the profile that this call chooses.
    No device profile has ever supplied ``paths.roms``, so nothing is lost by
    looking one step early.

    Falls back to :data:`DEFAULT_PROFILE` whenever the card does not say --
    unplugged, unreadable, or a board newer than the mapping.  ``auto`` is a
    request for a better default, not an assertion that one exists.
    """
    from .detect import detect_profiles  # Imported late: `detect` imports `devices`, not `config`.

    raw = _highest(layers, "paths", "roms")
    if not isinstance(raw, (str, Path)):
        return DEFAULT_PROFILE
    volume = Path(raw).expanduser().parent
    profiles = detect_profiles(volume, boot_partition_beside(volume))
    return profiles[0].id if profiles else DEFAULT_PROFILE


def load_config(
    *,
    config_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
) -> Config:
    """Assemble a :class:`Config` from every layer, in precedence order."""
    file_path = find_config_file(config_path)
    file_layer = _read_yaml(file_path) if file_path is not None else {}
    env_layer = _env_layer() if use_env else {}
    cli_layer = overrides or {}

    # The device profile is chosen by the higher layers, then contributes the
    # defaults they are merged on top of.
    layers = (file_layer, env_layer, cli_layer)
    device: Any = DEFAULT_PROFILE
    for layer in layers:
        if layer.get("device") is not None:
            device = layer["device"]
    if device == AUTO_DEVICE:
        device = _detect_device(layers)

    merged: dict[str, Any] = profile_defaults(device if isinstance(device, str) else None)
    for layer in (file_layer, env_layer, cli_layer):
        merged = _deep_merge(merged, layer)
    merged["device"] = device

    try:
        config = Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_explain(exc, file_path)) from exc

    config.register_secrets()
    return config


def _explain(exc: ValidationError, source: Path | None) -> str:
    where = f" in {source}" if source else ""
    lines = [f"Invalid configuration{where}:"]
    for error in exc.errors():
        location = ".".join(str(p) for p in error["loc"]) or "(root)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def save_config(config: Config, path: Path) -> Path:
    """Write the config as YAML with ``0600`` permissions.

    The file holds two passwords, so the mode is set *before* the content is
    written -- creating it world-readable and tightening afterwards would leave
    a window where the secrets are exposed.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = config.model_dump(mode="json", exclude_none=False)
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)

    atomic_text(path, text, mode=0o600)
    return path
