"""System table: front-end folder name <-> ScreenScraper ``systemeid``.

.. warning::
   The ``systemeid`` values below are hand-curated from published mappings, not
   generated from a live API call.  Run ``pyskraper systems --verify`` to check the
   whole table against ``systemesListe.php``; it reports any id whose name does
   not match what the server says.  Treat that command as the source of truth
   and this table as a cache of it.

Archive policy is the other load-bearing column, and it is not cosmetic.
ScreenScraper's arcade entries are keyed on the hash of the ``.zip`` itself,
because that is what MAME actually loads.  Console entries are keyed on the
hash of the raw ROM, because that is what No-Intro catalogues -- so for a zipped
SNES ROM we must hash the file *inside* the archive.  Hashing the wrong one is
a guaranteed miss, and a miss spends the scarce KO quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["SYSTEMS", "SystemInfo", "lookup"]

ArchivePolicy = Literal["archive", "contents"]


@dataclass(frozen=True)
class SystemInfo:
    folder: str
    systeme_id: int
    label: str
    extensions: frozenset[str]
    archive_policy: ArchivePolicy = "contents"
    disc: bool = False
    arcade: bool = False
    aliases: tuple[str, ...] = ()


def _s(
    folder: str,
    systeme_id: int,
    label: str,
    extensions: str,
    *,
    archive_policy: ArchivePolicy = "contents",
    disc: bool = False,
    arcade: bool = False,
    aliases: tuple[str, ...] = (),
) -> SystemInfo:
    return SystemInfo(
        folder=folder,
        systeme_id=systeme_id,
        label=label,
        extensions=frozenset(e.strip().lower() for e in extensions.split() if e.strip()),
        archive_policy=archive_policy,
        disc=disc,
        arcade=arcade,
        aliases=aliases,
    )


_DISC = ".chd .cue .iso .m3u .pbp .bin .ccd .img .mdf .gdi"

SYSTEMS: tuple[SystemInfo, ...] = (
    # --- Nintendo ---
    _s("nes", 3, "Nintendo Entertainment System", ".nes .unf .unif .fds .zip .7z", aliases=("famicom",)),
    _s("fds", 106, "Famicom Disk System", ".fds .zip .7z"),
    _s("snes", 4, "Super Nintendo", ".sfc .smc .fig .swc .zip .7z", aliases=("sfc", "supernes")),
    _s("satellaview", 107, "Satellaview", ".sfc .smc .bs .zip .7z"),
    _s("sufami", 108, "Sufami Turbo", ".st .zip .7z"),
    _s("n64", 14, "Nintendo 64", ".z64 .n64 .v64 .zip .7z"),
    _s("gb", 9, "Game Boy", ".gb .zip .7z"),
    _s("gbc", 10, "Game Boy Color", ".gbc .gb .zip .7z"),
    _s("gba", 12, "Game Boy Advance", ".gba .zip .7z"),
    _s("virtualboy", 11, "Virtual Boy", ".vb .vboy .zip .7z", aliases=("vb",)),
    _s("nds", 15, "Nintendo DS", ".nds .zip .7z"),
    _s("pokemini", 211, "Pokemon Mini", ".min .zip .7z"),
    _s("gw", 52, "Game & Watch", ".mgw .zip .7z", aliases=("gameandwatch",)),
    # --- Sega ---
    _s("megadrive", 1, "Sega Mega Drive / Genesis", ".md .gen .bin .smd .zip .7z", aliases=("genesis",)),
    _s("mastersystem", 2, "Sega Master System", ".sms .zip .7z", aliases=("sms",)),
    _s("gamegear", 21, "Sega Game Gear", ".gg .zip .7z", aliases=("gg",)),
    _s("sg1000", 109, "Sega SG-1000", ".sg .zip .7z", aliases=("sg-1000",)),
    _s("sega32x", 19, "Sega 32X", ".32x .bin .zip .7z", aliases=("32x",)),
    _s("segacd", 20, "Sega CD / Mega-CD", _DISC, disc=True, aliases=("megacd",)),
    _s("saturn", 22, "Sega Saturn", _DISC, disc=True),
    _s("dreamcast", 23, "Sega Dreamcast", ".chd .cdi .gdi .m3u .cue", disc=True, aliases=("dc",)),
    # --- Sony ---
    _s("psx", 57, "Sony PlayStation", _DISC, disc=True, aliases=("ps1", "playstation")),
    _s("ps2", 58, "Sony PlayStation 2", _DISC, disc=True),
    _s("psp", 61, "Sony PSP", ".iso .cso .chd .pbp", disc=True),
    # --- NEC ---
    _s("pcengine", 31, "PC Engine / TurboGrafx-16", ".pce .zip .7z", aliases=("tg16",)),
    _s("pcenginecd", 114, "PC Engine CD", _DISC, disc=True, aliases=("tg-cd",)),
    _s("supergrafx", 105, "SuperGrafx", ".sgx .pce .zip .7z"),
    _s("pcfx", 72, "PC-FX", _DISC, disc=True),
    # --- SNK ---
    _s("neogeo", 142, "Neo Geo", ".zip .7z", archive_policy="archive", arcade=True),
    _s("neogeocd", 70, "Neo Geo CD", _DISC, disc=True),
    _s("ngp", 25, "Neo Geo Pocket", ".ngp .zip .7z"),
    _s("ngpc", 82, "Neo Geo Pocket Color", ".ngc .ngpc .zip .7z"),
    # --- Arcade: hashed as the archive, because that is what the emulator loads ---
    _s("arcade", 75, "Arcade", ".zip .7z", archive_policy="archive", arcade=True),
    _s("mame", 75, "MAME", ".zip .7z", archive_policy="archive", arcade=True),
    _s("fbneo", 75, "FinalBurn Neo", ".zip .7z", archive_policy="archive", arcade=True, aliases=("fba", "fbn")),
    _s("naomi", 56, "Sega Naomi", ".zip .7z .chd", archive_policy="archive", arcade=True),
    _s("atomiswave", 53, "Atomiswave", ".zip .7z .chd", archive_policy="archive", arcade=True),
    # --- Atari ---
    _s("atari2600", 26, "Atari 2600", ".a26 .bin .zip .7z"),
    _s("atari5200", 40, "Atari 5200", ".a52 .bin .zip .7z"),
    _s("atari7800", 41, "Atari 7800", ".a78 .bin .zip .7z"),
    _s("atari800", 43, "Atari 800", ".atr .xex .zip .7z"),
    _s("atarist", 42, "Atari ST", ".st .stx .msa .zip .7z"),
    _s("lynx", 28, "Atari Lynx", ".lnx .zip .7z", aliases=("atarilynx",)),
    _s("jaguar", 27, "Atari Jaguar", ".j64 .jag .zip .7z", aliases=("atarijaguar",)),
    # --- Computers & the rest ---
    _s("amiga500", 64, "Commodore Amiga 500", ".adf .lha .zip .7z", aliases=("amiga",)),
    _s("amiga1200", 64, "Commodore Amiga 1200", ".adf .lha .zip .7z"),
    _s("amigacd32", 130, "Commodore Amiga CD32", _DISC, disc=True),
    _s("c64", 66, "Commodore 64", ".d64 .t64 .prg .crt .zip .7z"),
    _s("vic20", 73, "Commodore VIC-20", ".d64 .prg .crt .zip .7z"),
    _s("zxspectrum", 76, "Sinclair ZX Spectrum", ".tzx .tap .z80 .sna .zip .7z"),
    _s("zx81", 77, "Sinclair ZX81", ".p .tzx .zip .7z"),
    _s("amstradcpc", 65, "Amstrad CPC", ".dsk .cdt .zip .7z"),
    _s("msx1", 113, "MSX", ".rom .mx1 .dsk .zip .7z", aliases=("msx",)),
    _s("msx2", 116, "MSX2", ".rom .mx2 .dsk .zip .7z"),
    _s("x68000", 79, "Sharp X68000", ".dim .d88 .zip .7z"),
    _s("colecovision", 48, "ColecoVision", ".col .rom .zip .7z", aliases=("coleco",)),
    _s("intellivision", 115, "Intellivision", ".int .bin .rom .zip .7z"),
    _s("vectrex", 102, "Vectrex", ".vec .bin .zip .7z"),
    _s("channelf", 80, "Fairchild Channel F", ".bin .chf .zip .7z"),
    _s("wonderswan", 45, "WonderSwan", ".ws .zip .7z", aliases=("wswan",)),
    _s("wonderswancolor", 46, "WonderSwan Color", ".wsc .zip .7z", aliases=("wswanc",)),
    _s("megaduck", 90, "Mega Duck", ".bin .zip .7z"),
    _s("3do", 29, "3DO", _DISC, disc=True),
    _s("scummvm", 123, "ScummVM", ".scummvm .svm"),
    _s("dos", 135, "MS-DOS", ".dosz .zip .7z .exe .bat"),
    _s("pico8", 234, "PICO-8", ".p8 .png"),
)

_BY_NAME: dict[str, SystemInfo] = {}
for _system in SYSTEMS:
    _BY_NAME[_system.folder] = _system
    for _alias in _system.aliases:
        _BY_NAME.setdefault(_alias, _system)


def lookup(folder: str) -> SystemInfo | None:
    """Resolve a ROM folder name (or a known alias) to its system."""
    return _BY_NAME.get(folder.strip().lower())
