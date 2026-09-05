"""Device profiles.

A profile supplies *defaults only*.  Anything set explicitly in the config file,
an environment variable, or a CLI flag always wins -- the profile is the bottom
layer above the built-in defaults, never an override.

There is a profile for every board KNULLI ships an image for, which is a wider
set than the devices knulli.org documents (see https://knulli.org/devices/) --
the rule is deliberate, because `pyskraper.detect` reads the board name off the
card, and a board with no profile behind it is a card the tool cannot recognise.

A profile carries one thing that matters: the panel resolution, which caps the
artwork so a 640x480 screen does not get 1600px box art it cannot show.  The
`notes` field records the hardware it came from so the numbers can be checked
against the vendor's own spec sheet rather than taken on trust.

Anything not listed -- a device newer than this file, or a desktop install --
uses `none` plus an explicit `images` size, which the wizard's "Other / custom
resolution" entry writes for you.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_PROFILE",
    "PROFILES",
    "DeviceProfile",
    "by_vendor",
    "get_profile",
    "profile_defaults",
]


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    name: str
    vendor: str = ""
    screen: tuple[int, int] | None = None
    notes: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)


def _screen(width: int, height: int) -> dict[str, Any]:
    """The defaults a panel of this size implies.

    Server-side resize is preferred because it saves the download, not just the
    disk: ScreenScraper shrinks before sending rather than after.
    """
    return {"images": {"max_width": width, "max_height": height, "prefer_server_resize": True}}


# Insertion order is the display order -- `by_vendor` groups on it, so the
# wizard and `pyskraper devices` present the same list without sorting.
PROFILES: dict[str, DeviceProfile] = {
    "anbernic-rg35xx-2024": DeviceProfile(
        id="anbernic-rg35xx-2024",
        name="RG35XX 2024",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Allwinner H700, 1 GB RAM, 3.5in 640x480 4:3 IPS, no built-in Wi-Fi.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rg35xx-plus": DeviceProfile(
        id="anbernic-rg35xx-plus",
        name="RG35XX Plus",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Allwinner H700, 1 GB RAM, 3.5in 640x480 4:3 IPS, Wi-Fi.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rg35xx-h": DeviceProfile(
        id="anbernic-rg35xx-h",
        name="RG35XX H",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Allwinner H700, 1 GB RAM, 3.5in 640x480 4:3 IPS, horizontal layout.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rg35xx-sp": DeviceProfile(
        id="anbernic-rg35xx-sp",
        name="RG35XX SP",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Allwinner H700, 1 GB RAM, 3.5in 640x480 4:3 IPS, clamshell.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rg35xx-pro": DeviceProfile(
        id="anbernic-rg35xx-pro",
        name="RG35XX Pro",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Allwinner H700, 1 GB RAM, 3.5in 640x480 4:3 IPS, Wi-Fi and HDMI out.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rg35xx": DeviceProfile(
        id="anbernic-rg35xx",
        name="RG35XX (original)",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Actions ATM7039S, 256 MB RAM, 3.5in 640x480 4:3 IPS. The weakest device here.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rg28xx": DeviceProfile(
        id="anbernic-rg28xx",
        name="RG28XX",
        vendor="Anbernic",
        screen=(640, 480),
        # The panel is physically portrait and rotated in software -- KNULLI's
        # `rcS` runs `fbset -g 480 640 ...` for this board alone, and its
        # bootlogo is 480x640.  640x480 is what ends up on screen, which is what
        # artwork is sized against, so do not "correct" this to match the panel.
        notes="Allwinner H700, 1 GB RAM, 2.83in 640x480 4:3 IPS.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rg34xx": DeviceProfile(
        id="anbernic-rg34xx",
        name="RG34XX",
        vendor="Anbernic",
        screen=(720, 480),
        notes="Allwinner H700, 1 GB RAM, 3.4in 720x480 3:2 IPS.",
        defaults=_screen(720, 480),
    ),
    "anbernic-rg34xx-sp": DeviceProfile(
        id="anbernic-rg34xx-sp",
        name="RG34XX SP",
        vendor="Anbernic",
        screen=(720, 480),
        notes="Allwinner H700, 2 GB RAM, 3.4in 720x480 3:2 IPS, clamshell.",
        defaults=_screen(720, 480),
    ),
    "anbernic-rg40xx-h": DeviceProfile(
        id="anbernic-rg40xx-h",
        name="RG40XX H",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Allwinner H700, 1 GB RAM, 4.0in 640x480 4:3 IPS, horizontal layout.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rg40xx-v": DeviceProfile(
        id="anbernic-rg40xx-v",
        name="RG40XX V",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Allwinner H700, 1 GB RAM, 4.0in 640x480 4:3 IPS, vertical layout.",
        defaults=_screen(640, 480),
    ),
    "anbernic-rgcubexx": DeviceProfile(
        id="anbernic-rgcubexx",
        name="RGCubeXX",
        vendor="Anbernic",
        screen=(720, 720),
        notes="Allwinner H700, 1 GB RAM, 3.95in 720x720 1:1 IPS.",
        defaults=_screen(720, 720),
    ),
    "anbernic-rg-arc-s": DeviceProfile(
        id="anbernic-rg-arc-s",
        name="RG Arc S",
        vendor="Anbernic",
        screen=(640, 480),
        notes="Rockchip RK3566, 1 GB RAM, 4.0in 640x480 4:3 IPS.",
        defaults=_screen(640, 480),
    ),
    "trimui-brick": DeviceProfile(
        id="trimui-brick",
        name="Brick",
        vendor="TrimUI",
        screen=(1024, 768),
        notes="Allwinner A133, 3.2in 1024x768 4:3 IPS.",
        defaults=_screen(1024, 768),
    ),
    "trimui-smart-pro": DeviceProfile(
        id="trimui-smart-pro",
        name="Smart Pro",
        vendor="TrimUI",
        screen=(1280, 720),
        notes="Allwinner A133 Plus, 4.96in 1280x720 16:9 IPS.",
        defaults=_screen(1280, 720),
    ),
    "trimui-smart-pro-s": DeviceProfile(
        id="trimui-smart-pro-s",
        name="Smart Pro S",
        vendor="TrimUI",
        screen=(1280, 720),
        notes="Allwinner A523/T527, 1 GB RAM, 4.96in 1280x720 16:9 IPS.",
        defaults=_screen(1280, 720),
    ),
    "miyoo-flip": DeviceProfile(
        id="miyoo-flip",
        name="Flip",
        vendor="Miyoo",
        screen=(640, 480),
        notes="Rockchip RK3566, 1 GB RAM, 3.5in 640x480 4:3 IPS, clamshell.",
        defaults=_screen(640, 480),
    ),
    "powkiddy-rgb30": DeviceProfile(
        id="powkiddy-rgb30",
        name="RGB30",
        vendor="Powkiddy",
        screen=(720, 720),
        notes="Rockchip RK3566, 4.0in 720x720 1:1 IPS.",
        defaults=_screen(720, 720),
    ),
    "powkiddy-x55": DeviceProfile(
        id="powkiddy-x55",
        name="X55",
        vendor="Powkiddy",
        screen=(1280, 720),
        notes="Rockchip RK3566, 2 GB RAM, 5.5in 1280x720 16:9 IPS.",
        defaults=_screen(1280, 720),
    ),
    "powkiddy-v20": DeviceProfile(
        id="powkiddy-v20",
        name="V20",
        vendor="Powkiddy",
        screen=(640, 480),
        notes="Allwinner A133 Plus, 1 GB RAM, 3.5in 640x480 4:3 IPS, vertical layout.",
        defaults=_screen(640, 480),
    ),
    "powkiddy-v90s": DeviceProfile(
        id="powkiddy-v90s",
        name="V90S",
        vendor="Powkiddy",
        screen=(640, 480),
        notes="Allwinner A133 Plus, 1 GB RAM, 3.5in 640x480 IPS, clamshell.",
        defaults=_screen(640, 480),
    ),
    "powkiddy-a13": DeviceProfile(
        id="powkiddy-a13",
        name="A13",
        vendor="Powkiddy",
        screen=(1024, 600),
        # KNULLI's own `extlinux.conf` for this board notes that Rev C and Rev D
        # hardware ships an 800x480 panel instead.  The shipped default is the
        # 1024x600 device tree, so that is what this profile assumes.
        notes="Rockchip RK3128, 1024x600 panel. Rev C/D hardware is 800x480 instead.",
        defaults=_screen(1024, 600),
    ),
    "retroid-pocket-5": DeviceProfile(
        id="retroid-pocket-5",
        name="Retroid Pocket 5",
        vendor="GoRetroid",
        screen=(1920, 1080),
        notes="Snapdragon 865, 8 GB RAM, 5.5in 1920x1080 16:9 AMOLED.",
        defaults=_screen(1920, 1080),
    ),
    "retroid-pocket-flip-2": DeviceProfile(
        id="retroid-pocket-flip-2",
        name="Retroid Pocket Flip 2",
        vendor="GoRetroid",
        screen=(1920, 1080),
        notes="Snapdragon 865, 5.5in 1920x1080 16:9 AMOLED, clamshell.",
        defaults=_screen(1920, 1080),
    ),
    "retroid-pocket-mini": DeviceProfile(
        id="retroid-pocket-mini",
        name="Retroid Pocket Mini",
        vendor="GoRetroid",
        screen=(1280, 960),
        notes="Snapdragon 865, 6 GB RAM, 3.7in 1280x960 4:3 AMOLED.",
        defaults=_screen(1280, 960),
    ),
    "retroid-pocket-mini-v2": DeviceProfile(
        id="retroid-pocket-mini-v2",
        name="Retroid Pocket Mini V2",
        vendor="GoRetroid",
        screen=(1280, 960),
        notes="Snapdragon 865, 3.7in 1280x960 4:3 AMOLED.",
        defaults=_screen(1280, 960),
    ),
    "magicx-xu-mini-m": DeviceProfile(
        id="magicx-xu-mini-m",
        name="XU Mini M",
        vendor="MagicX",
        screen=(640, 480),
        notes="Rockchip RK3326, 1 GB RAM, 2.8in 640x480 4:3 IPS.",
        defaults=_screen(640, 480),
    ),
    "gkd-pixel-2": DeviceProfile(
        id="gkd-pixel-2",
        name="Pixel 2",
        vendor="GKD",
        screen=(640, 480),
        notes="Rockchip RK3326S, 1 GB RAM, 2.4in 640x480 4:3 IPS.",
        defaults=_screen(640, 480),
    ),
    "batlexp-g350": DeviceProfile(
        id="batlexp-g350",
        name="G350",
        vendor="BatleXP",
        screen=(640, 480),
        notes="Rockchip RK3326, 1 GB RAM, 3.5in 640x480 4:3 IPS, vertical layout.",
        defaults=_screen(640, 480),
    ),
    "r36s": DeviceProfile(
        id="r36s",
        name="R36S",
        vendor="Unbranded",
        screen=(640, 480),
        notes="Rockchip RK3326, 1 GB RAM, 3.5in 640x480 4:3 IPS. Sold under many names.",
        defaults=_screen(640, 480),
    ),
    "ps5000": DeviceProfile(
        id="ps5000",
        name="PS5000",
        vendor="Unbranded",
        screen=(960, 544),
        notes="Rockchip RK3128, 5in 960x544 panel. Sold under many names.",
        defaults=_screen(960, 544),
    ),
    "ps7000": DeviceProfile(
        id="ps7000",
        name="PS7000",
        vendor="Unbranded",
        screen=(1024, 600),
        notes="Rockchip RK3128, 7in 1024x600 panel. Sold under many names.",
        defaults=_screen(1024, 600),
    ),
    "orange-pi-zero-2w": DeviceProfile(
        id="orange-pi-zero-2w",
        name="Zero 2w",
        vendor="Orange Pi",
        notes="Allwinner H618 single-board computer. HDMI out, so no panel to size for.",
        defaults={},
    ),
    "none": DeviceProfile(
        id="none",
        name="No device profile",
        notes="No screen assumptions -- full-size artwork unless you set images yourself.",
        defaults={},
    ),
}

DEFAULT_PROFILE = "anbernic-rg35xx-2024"


def by_vendor() -> dict[str, list[DeviceProfile]]:
    """Profiles grouped by vendor, both groups and members in `PROFILES` order.

    `none` is left out: it is not a device, and the wizard reaches it through
    "Other / custom resolution" rather than through a vendor.
    """
    groups: dict[str, list[DeviceProfile]] = {}
    for profile in PROFILES.values():
        if not profile.vendor:
            continue
        groups.setdefault(profile.vendor, []).append(profile)
    return groups


def get_profile(name: str | None) -> DeviceProfile:
    """Look a profile up by id.  Unknown names raise with the valid list."""
    if not name:
        return PROFILES["none"]
    try:
        return PROFILES[name]
    except KeyError:
        valid = ", ".join(sorted(PROFILES))
        raise KeyError(f"Unknown device profile {name!r}. Known profiles: {valid}") from None


def profile_defaults(name: str | None) -> dict[str, Any]:
    """The defaults a profile contributes, as a merge-ready mapping."""
    return copy.deepcopy(get_profile(name).defaults)
