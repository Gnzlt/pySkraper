"""Device profiles.

The roster is data, and data rots: a screen size that drifts from the hardware
produces artwork that is quietly the wrong size on someone's card, with nothing
to show for it in a stack trace.  So the checks here are mostly about the table
staying internally consistent and every entry staying reachable from the wizard.
"""

from __future__ import annotations

import pytest

from pyskraper.devices import (
    DEFAULT_PROFILE,
    PROFILES,
    DeviceProfile,
    by_vendor,
    get_profile,
    profile_defaults,
)

# Devices with no panel of their own: an SBC on HDMI, and the escape hatch for
# anything not listed.  Everything else must know how big its screen is.
_SCREENLESS = {"orange-pi-zero-2w", "none"}


def test_the_default_profile_exists() -> None:
    assert DEFAULT_PROFILE in PROFILES


_EVERY_PROFILE = pytest.mark.parametrize("profile", PROFILES.values(), ids=lambda p: p.id)


@_EVERY_PROFILE
def test_every_profile_is_internally_consistent(profile: DeviceProfile) -> None:
    assert profile.id in PROFILES
    assert profile.name
    assert profile.notes, "a profile with no notes cannot be checked against a spec sheet"


@_EVERY_PROFILE
def test_the_screen_and_the_image_defaults_agree(profile: DeviceProfile) -> None:
    """`screen` is what the wizard prints; `defaults` is what actually resizes."""
    images = profile_defaults(profile.id).get("images", {})
    if profile.id in _SCREENLESS:
        assert profile.screen is None
        assert images == {}
        return
    assert profile.screen is not None
    assert (images.get("max_width"), images.get("max_height")) == profile.screen


@_EVERY_PROFILE
def test_a_profile_carries_nothing_but_its_screen(profile: DeviceProfile) -> None:
    """Systems and output format are not the device's business any more."""
    assert set(profile_defaults(profile.id)) <= {"images"}


def test_every_device_is_reachable_through_a_vendor() -> None:
    grouped = {p.id for profiles in by_vendor().values() for p in profiles}
    assert grouped == set(PROFILES) - {"none"}, "only `none` is reached outside the vendor lists"


def test_each_vendors_devices_are_declared_together() -> None:
    """A vendor's block has to be contiguous in `PROFILES`.

    `by_vendor` collects a vendor's devices into the group it opened the first
    time it saw that name, so declaring one Powkiddy at the top of the file and
    another at the bottom puts them in one group but out of file order -- the
    wizard then lists them in an order the source does not show.  Failing here
    is the cheap version of that surprise.
    """
    seen: list[str] = []
    for profile in PROFILES.values():
        if not profile.vendor:
            continue
        if seen and seen[-1] == profile.vendor:
            continue
        assert profile.vendor not in seen, (
            f"{profile.id!r} is declared away from the other {profile.vendor} devices. "
            f"Move it next to them in PROFILES."
        )
        seen.append(profile.vendor)


def test_vendor_grouping_keeps_the_declaration_order() -> None:
    """The wizard prints these in dict order and does no sorting of its own."""
    flattened = [p.id for profiles in by_vendor().values() for p in profiles]
    declared = [p.id for p in PROFILES.values() if p.vendor]
    assert flattened == declared


def test_the_default_profile_is_the_first_model_of_its_brand() -> None:
    """Both wizard prompts default to it, so it has to be findable in the list."""
    default = PROFILES[DEFAULT_PROFILE]
    assert by_vendor()[default.vendor][0] is default


def test_an_unknown_profile_names_the_valid_ones() -> None:
    with pytest.raises(KeyError, match="anbernic-rg35xx-2024"):
        get_profile("no-such-handheld")


def test_no_name_repeats_its_vendor() -> None:
    """The wizard prints the brand as a heading, so "Anbernic RG28XX" would stutter."""
    for profile in PROFILES.values():
        assert not profile.name.startswith(profile.vendor or "\0")
