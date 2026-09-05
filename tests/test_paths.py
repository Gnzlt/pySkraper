"""Where state lands.

The point of this module is a promise: pySkraper writes inside its own folder
and nowhere else.  That promise is only worth what its tests are worth, so the
resolution order gets a case each, and the "nothing escapes the base directory"
guarantee gets an explicit guard rather than a convention.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from pyskraper import paths


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No --data-dir, no $PYSKRAPER_HOME, unless a test asks for one."""
    paths.set_base_dir(None)
    monkeypatch.delenv("PYSKRAPER_HOME", raising=False)
    yield
    paths.set_base_dir(None)


# --------------------------------------------------------------------------
# base_dir resolution order
# --------------------------------------------------------------------------


def test_explicit_data_dir_wins_over_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYSKRAPER_HOME", str(tmp_path / "from-env"))
    paths.set_base_dir(tmp_path / "from-flag")

    assert paths.base_dir() == (tmp_path / "from-flag").resolve()


def test_env_var_wins_over_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYSKRAPER_HOME", str(tmp_path / "from-env"))

    assert paths.base_dir() == (tmp_path / "from-env").resolve()


def test_project_root_is_used_when_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "pySkraper"
    project.mkdir()
    monkeypatch.setattr(paths, "_project_root", lambda: project)

    assert paths.base_dir() == project


def test_falls_back_to_cwd_when_project_root_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy on a read-only volume still has to run."""
    project = tmp_path / "readonly"
    project.mkdir()
    original = project.stat().st_mode
    project.chmod(stat.S_IRUSR | stat.S_IXUSR)
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.setattr(paths, "_project_root", lambda: project)
    monkeypatch.chdir(working)

    try:
        assert paths.base_dir() == working.resolve()
    finally:
        project.chmod(original)


def test_installed_package_never_writes_to_site_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The writability probe would succeed in a virtualenv -- that is the trap.

    A venv's site-packages is perfectly writable, so a probe alone would happily
    scatter config and a cache database through the Python environment, where an
    uninstall would not remove them.
    """
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.setattr(paths, "_project_root", lambda: site)
    monkeypatch.chdir(working)

    assert paths.is_writable(site), "precondition: the probe alone would allow this"
    assert paths.base_dir() == working.resolve()


def test_dist_packages_is_treated_the_same_as_site_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "usr" / "lib" / "python3" / "dist-packages"
    dist.mkdir(parents=True)
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.setattr(paths, "_project_root", lambda: dist)
    monkeypatch.chdir(working)

    assert paths.base_dir() == working.resolve()


# --------------------------------------------------------------------------
# Everything else hangs off base_dir
# --------------------------------------------------------------------------


def test_every_path_is_inside_the_base_directory(tmp_path: Path) -> None:
    paths.set_base_dir(tmp_path)

    for candidate in (paths.config_path(), paths.cache_dir(), paths.cache_path(paths.cache_dir())):
        assert candidate.is_relative_to(tmp_path), candidate


def test_config_is_named_for_the_project(tmp_path: Path) -> None:
    paths.set_base_dir(tmp_path)

    assert paths.config_path() == tmp_path / "pyskraper.yaml"


def test_no_default_path_points_at_a_system_location(tmp_path: Path) -> None:
    """The regression this whole module exists to prevent."""
    paths.set_base_dir(tmp_path)
    home = Path.home()

    for candidate in (paths.config_path(), paths.cache_dir(), paths.cache_path(paths.cache_dir())):
        assert not candidate.is_relative_to(home / ".config"), candidate
        assert not candidate.is_relative_to(home / "Library"), candidate
        assert not candidate.is_relative_to(home / ".cache"), candidate


# --------------------------------------------------------------------------
# is_writable
# --------------------------------------------------------------------------


def test_is_writable_probes_rather_than_trusting_the_mode(tmp_path: Path) -> None:
    writable = tmp_path / "yes"
    writable.mkdir()
    assert paths.is_writable(writable)


def test_is_writable_is_false_for_a_read_only_directory(tmp_path: Path) -> None:
    if os.getuid() == 0:  # pragma: no cover - root ignores the permission bits
        pytest.skip("running as root: permission bits do not apply")
    locked = tmp_path / "no"
    locked.mkdir()
    original = locked.stat().st_mode
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert not paths.is_writable(locked)
    finally:
        locked.chmod(original)


def test_is_writable_is_false_for_a_missing_directory(tmp_path: Path) -> None:
    assert not paths.is_writable(tmp_path / "nope")


def test_the_probe_leaves_nothing_behind(tmp_path: Path) -> None:
    paths.is_writable(tmp_path)

    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# Finding a card, on both platforms, from either platform
# --------------------------------------------------------------------------


def test_macos_looks_under_volumes() -> None:
    assert paths.candidate_mount_roots("darwin") == [Path("/Volumes")]


def test_linux_prefers_the_per_user_media_directories() -> None:
    roots = paths.candidate_mount_roots("linux")

    assert Path("/mnt") in roots
    assert Path("/media") in roots
    # The per-user directories are where a desktop session actually mounts a
    # card, so they have to be searched before the shared fallbacks.
    per_user = [r for r in roots if r.parent in (Path("/run/media"), Path("/media"))]
    assert per_user, "expected /run/media/<user> and /media/<user>"
    assert roots.index(per_user[0]) < roots.index(Path("/media"))


def test_an_unknown_platform_offers_nothing_rather_than_guessing() -> None:
    assert paths.candidate_mount_roots("sunos5") == []


def test_mount_roots_returns_only_directories_that_exist() -> None:
    for root in paths.mount_roots():
        assert root.is_dir()
