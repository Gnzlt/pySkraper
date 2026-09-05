"""The guided setup, as a script.

Someone who has never opened the README should be able to run ``pyskraper``,
answer a handful of questions, and end up with a scraped card.  That is the
whole brief, and it rules out a lot: no full-screen layout, no cursor-addressed
panes, no space-to-toggle grids.  Questions print, you answer, the next one
prints.  It works the same over ssh, in a terminal that has been resized, and
in a scrollback buffer you read afterwards.

Each step is a function that takes the config and mutates one part of it.  They
run in sequence, but nothing binds them together beyond that -- a step that the
config already answers prints one line and moves on, so re-running the wizard
to change a single setting is not a chore.

Two rules the tests pin down: Ctrl-C anywhere leaves nothing written, and no
prompt ever appears when there is no one there to answer it.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .api.errors import AuthError, ScreenScraperError
from .api.quota import QuotaSnapshot
from .banner import print_banner
from .config import DEFAULT_MEDIA, RECOMMENDED_MEDIA, Config, ConfigError, load_config, save_config
from .core.scanner import ScannedSystem, scan_tree
from .detect import detect_profiles
from .devices import DEFAULT_PROFILE, PROFILES, DeviceProfile, by_vendor, profile_defaults
from .exits import EXIT_CONFIG, EXIT_INTERRUPTED, EXIT_OK
from .paths import config_path, is_boot_volume, mount_roots
from .systems import lookup
from .theme import WIZARD
from .ui import format_bytes, free_bytes

__all__ = ["CardCandidate", "find_cards", "run_wizard"]

console = Console(theme=WIZARD, highlight=False)

# Media tags that dominate the byte cost.  Worth saying out loud at the moment
# someone is choosing a preset, rather than in a cost report they see later.
_HEAVY_TAGS = ("video", "mix", "manual", "magazine")


def _rule(number: int, title: str) -> None:
    console.print(f"\n[step]{number}[/]  [title]{title}[/]")


def _note(text: str) -> None:
    console.print(f"   {text}")


def _option(index: int, largest: int, label: str, detail: str = "", *, selected: bool = False) -> None:
    """One line of a numbered list, with the numbers right-aligned.

    Right-aligning matters once a list runs past nine: ragged indices push the
    labels out of line, and the eye stops being able to scan down the column.
    """
    marker = "[marker]>[/]" if selected else " "
    number = str(index).rjust(len(str(largest)))
    trailing = f"  [dim]{detail}[/]" if detail else ""
    console.print(f"   {marker} [key]{number}[/]  {label}{trailing}")


# --------------------------------------------------------------------------
# Finding a card
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CardCandidate:
    """A ``roms/`` directory we found, and why we think it is the right one."""

    roms: Path
    volume: Path
    known_systems: int
    total_systems: int
    boot_partition: Path | None = None

    @property
    def looks_like_a_handheld_card(self) -> bool:
        return self.boot_partition is not None

    @property
    def rank(self) -> tuple[int, int]:
        """Sort key, descending: recognised system folders, then any folders.

        Deliberately ignores :attr:`boot_partition`, which looks like it should
        help and does not.  Volumes are siblings -- on macOS everything mounts
        under ``/Volumes`` -- so a ``KNULLI`` partition sits beside *every*
        candidate equally and separates none of them.  It says a handheld card
        is plugged in, which is worth knowing for the device profile, but it
        cannot tell you which of two ``roms`` folders is the one you want.
        """
        return (self.known_systems, self.total_systems)

    def describe(self) -> str:
        bits = [f"{self.known_systems} known system(s)"]
        if self.total_systems > self.known_systems:
            bits.append(f"{self.total_systems - self.known_systems} unrecognised")
        if self.boot_partition is not None:
            bits.append(f"boot partition {self.boot_partition.name}")
        return " · ".join(bits)


def _inspect(roms: Path, volume: Path, siblings: list[Path]) -> CardCandidate | None:
    """Describe a ``roms/`` directory, or ``None`` if it is not one."""
    try:
        folders = [entry for entry in roms.iterdir() if entry.is_dir() and not entry.name.startswith(".")]
    except OSError:
        # An unreadable mount is not a candidate; it is also not an error worth
        # stopping for, since the user can always type a path.
        return None
    if not folders:
        return None

    known = sum(1 for folder in folders if lookup(folder.name) is not None)
    boot = next((s for s in siblings if s != volume and is_boot_volume(s)), None)
    return CardCandidate(
        roms=roms,
        volume=volume,
        known_systems=known,
        total_systems=len(folders),
        boot_partition=boot,
    )


def find_cards() -> list[CardCandidate]:
    """Every plausible ROM library on this machine, best first.

    Looks under the platform's removable-media roots, plus the working
    directory.  Deliberately does not search the whole home directory: a slow
    recursive walk at startup is a worse experience than typing a path.
    """
    candidates: list[CardCandidate] = []
    seen: set[Path] = set()

    for root in mount_roots():
        try:
            volumes = sorted(entry for entry in root.iterdir() if entry.is_dir())
        except OSError:
            continue
        for volume in volumes:
            roms = volume / "roms"
            if not roms.is_dir() or roms in seen:
                continue
            seen.add(roms)
            found = _inspect(roms, volume, volumes)
            if found is not None:
                candidates.append(found)

    local = Path.cwd() / "roms"
    if local.is_dir() and local not in seen:
        found = _inspect(local, Path.cwd(), [])
        if found is not None:
            candidates.append(found)

    candidates.sort(key=lambda c: c.rank, reverse=True)
    return candidates


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def _ask_text(question: str, current: str) -> str:
    """Prompt, offering the current value as the Enter default when there is one."""
    if current:
        return str(Prompt.ask(question, default=current, console=console))
    return str(Prompt.ask(question, console=console))


def _ask_secret(question: str, current: str) -> str:
    """Prompt for a password without ever putting the old one on screen.

    ``Prompt.ask(default=...)`` renders the default in the prompt line, which
    for a password would print the stored secret in clear text -- so an existing
    value is offered as "leave blank to keep" instead of as a default.
    """
    if current:
        answer = str(
            Prompt.ask(f"{question} [dim](blank keeps the saved one)[/]", password=True, default="", console=console)
        )
        return answer or current
    return str(Prompt.ask(question, password=True, console=console))


def step_credentials(config: Config, *, verify: bool = True) -> QuotaSnapshot | None:
    """Collect credentials and prove they work before accepting them.

    Verification is not politeness.  A typo in ``devpassword`` produces a 403
    on the first real request, several minutes into a run, after the library has
    already been hashed -- so it is checked here, where the fix is one prompt
    away.
    """
    _rule(1, "ScreenScraper credentials")
    creds = config.screenscraper

    if creds.is_complete():
        _note(f"Developer ID [value]{creds.devid}[/] already configured.")
        if not Confirm.ask("   Change it?", default=False, console=console):
            return _verify(config) if verify else None

    _note("A developer account is required. Register at [url]https://www.screenscraper.fr[/]")
    _note("A member account is optional but raises the allowance a lot.\n")

    while True:
        creds.devid = _ask_text("   Developer ID", creds.devid)
        creds.devpassword = _ask_secret("   Developer password", creds.devpassword)

        if Confirm.ask(
            "   Do you have a ScreenScraper member account?", default=creds.has_user_account, console=console
        ):
            creds.ssid = _ask_text("   Username", creds.ssid)
            creds.sspassword = _ask_secret("   Password", creds.sspassword)
        else:
            creds.ssid = ""
            creds.sspassword = ""

        missing = creds.missing()
        if missing:
            console.print(f"   [danger]Missing:[/] {', '.join(missing)}. Both developer fields are required.")
            continue

        config.register_secrets()
        if not verify:
            return None
        snapshot = _verify(config)
        if snapshot is not None:
            return snapshot
        # _verify already explained the failure; loop and let them fix it.


def _verify(config: Config) -> QuotaSnapshot | None:
    """One round trip that both proves the login and reports the allowance."""
    from .cli import _check_api  # Imported here to keep the CLI's import graph acyclic.

    _note("Checking with ScreenScraper…")
    try:
        snapshot = asyncio.run(_check_api(config))
    except AuthError as exc:
        console.print(f"   [danger]Rejected:[/] {exc}")
        console.print("   [dim]Developer credentials are the devid/devpassword pair, not your login.[/]")
        return None
    except ScreenScraperError as exc:
        console.print(f"   [danger]Could not check:[/] {type(exc).__name__}: {exc}")
        return None
    except ConfigError as exc:
        console.print(f"   [danger]{exc}[/]")
        return None

    if config.screenscraper.has_user_account:
        who = snapshot.username or config.screenscraper.ssid
        console.print(f"   [ok]Verified[/] as [value]{who}[/] — {snapshot.max_threads} thread(s)")
    else:
        console.print(
            f"   [ok]Verified[/] (anonymous mode, developer credentials only) — "
            f"{snapshot.max_threads} thread, {snapshot.max_download_speed} kb/s downloads"
        )
    if snapshot.max_requests_per_day:
        left = snapshot.max_requests_per_day - snapshot.requests_today
        console.print(f"   {left:,} of {snapshot.max_requests_per_day:,} requests left today")
    return snapshot


def step_roms(config: Config) -> Path:
    """Find the card, or let the user say where it is."""
    _rule(2, "ROM location")
    current = config.paths.roms

    if current is not None and current.is_dir():
        _note(f"Using [value]{current}[/]")
        if not Confirm.ask("   Change it?", default=False, console=console):
            return current

    candidates = find_cards()
    if candidates:
        _note(f"Found {len(candidates)} possible location(s):\n")
        for index, candidate in enumerate(candidates, start=1):
            marker = "[marker]>[/]" if index == 1 else " "
            console.print(f"   {marker} [key]{index}[/]  {candidate.roms}")
            console.print(f"        [dim]{candidate.describe()}[/]")
        console.print(f"     [key]{len(candidates) + 1}[/]  Type a path instead\n")

        choice = _ask_index("   Which one?", len(candidates) + 1, default=1)
        if choice <= len(candidates):
            chosen = candidates[choice - 1].roms
            config.paths.roms = chosen
            return chosen
    else:
        _note("No SD card found automatically.")
        if not mount_roots():
            _note("[dim]This platform has no known removable-media directory to search.[/]")

    while True:
        answer = Prompt.ask("   Path to your roms folder", default=str(current) if current else None, console=console)
        path = Path(answer or "").expanduser()
        if path.is_dir():
            config.paths.roms = path
            return path
        console.print(f"   [danger]Not a directory:[/] {path}")


def step_device(config: Config, card: CardCandidate | None) -> None:
    """Pick a device, brand first -- unless the card already said which one.

    KNULLI writes its board name onto the card, so most of the time this step
    has the answer before it asks anything, and only needs it confirmed.  When
    it does not, there are thirty-odd handhelds to choose between, which is too
    many for one numbered list: it asks twice, brand and then model.

    The profile only supplies a default artwork size, so getting it wrong costs
    card space, not correctness.
    """
    _rule(3, "Device")

    detected = _detected_profiles(card)
    if detected and _offer_detected(config, detected[0], detected[1:]):
        return

    groups = by_vendor()
    default = detected[0] if detected else PROFILES[DEFAULT_PROFILE]
    vendors = list(groups)

    other = len(vendors) + 1
    default_index = vendors.index(default.vendor) + 1 if default.vendor in groups else 1
    width = max(len(v) for v in vendors)
    for index, vendor in enumerate(vendors, start=1):
        count = len(groups[vendor])
        tally = f"{count} model{'s' if count != 1 else ''}"
        _option(index, other, vendor.ljust(width), tally, selected=index == default_index)
    _option(other, other, "Other / custom resolution", "", selected=False)
    console.print()

    brand = _ask_index("   Which brand?", other, default=default_index)
    if brand == other:
        _custom_device(config)
        return

    models = groups[vendors[brand - 1]]
    model_default = models.index(default) + 1 if default in models else 1
    width = max(len(p.name) for p in models)
    console.print()
    for index, profile in enumerate(models, start=1):
        screen = f"{profile.screen[0]}x{profile.screen[1]}" if profile.screen else ""
        _option(index, len(models), profile.name.ljust(width), screen, selected=index == model_default)
    console.print()

    chosen = models[_ask_index("   Which model?", len(models), default=model_default) - 1]
    config.device = chosen.id
    _apply_profile(config, chosen)
    if chosen.notes:
        _note(f"[dim]{chosen.notes}[/]")


def _detected_profiles(card: CardCandidate | None) -> tuple[DeviceProfile, ...]:
    """What the card says it is, or an empty tuple if it says nothing."""
    if card is None:
        return ()
    return detect_profiles(card.volume, card.boot_partition)


def _offer_detected(config: Config, profile: DeviceProfile, siblings: tuple[DeviceProfile, ...]) -> bool:
    """Offer the detected device.  True when the user took it.

    Declining is not a dead end: the caller falls through to the ordinary
    brand-then-model lists with this profile preselected, so "no, but it is
    close" costs two keystrokes rather than starting the step over.
    """
    screen = f" — {profile.screen[0]}x{profile.screen[1]}" if profile.screen else ""
    _note(f"Detected from the card: [value]{profile.vendor} {profile.name}[/]{screen}")
    if siblings:
        # Kept short deliberately: `_note` indents the first line only, so a
        # message that wraps at 80 columns has its continuation hanging off the
        # left margin.  Naming the sibling is the point; explaining KNULLI's
        # image layout is not.
        shared = " and the ".join(f"{s.vendor} {s.name}" for s in siblings)
        same = ", and the same screen" if all(s.screen == profile.screen for s in siblings) else ""
        _note(f"[dim]Same KNULLI image as the {shared}{same}.[/]")
    console.print()

    if not Confirm.ask("   Use this device?", default=True, console=console):
        console.print()
        return False

    config.device = profile.id
    _apply_profile(config, profile)
    if profile.notes:
        _note(f"[dim]{profile.notes}[/]")
    return True


def _custom_device(config: Config) -> None:
    """Anything not on the list: no profile, just the screen size typed in.

    ``device: none`` plus an explicit ``images`` block is the same shape someone
    configuring by hand would write, so it round-trips through a saved config
    with no profile defaults to argue with.
    """
    config.device = "none"
    console.print()
    size = _ask_resolution("   Screen resolution, e.g. 640x480 (Enter for full-size artwork)")
    # Clear either way: the caps still sitting in the config came from the
    # default profile, and inheriting a screen size from a device the user has
    # just said they do not own is exactly the silent wrong answer to avoid.
    config.images.max_width, config.images.max_height = size or (None, None)
    if size is None:
        _note("[dim]No screen size set — artwork is downloaded at full size.[/]")
    else:
        _note(f"[dim]Artwork capped at {size[0]}x{size[1]}.[/]")


def _apply_profile(config: Config, profile: DeviceProfile) -> None:
    """Adopt the profile's defaults for the settings the wizard goes on to ask about.

    Only the fields later steps present: the profile is a starting point the
    user then edits, so overwriting anything they have not been shown would be
    changing settings behind their back.
    """
    defaults = profile_defaults(profile.id)
    for key, value in defaults.get("images", {}).items():
        setattr(config.images, key, value)


def step_systems(config: Config, roms: Path) -> list[ScannedSystem]:
    """Choose what to scrape, from what is actually on the card."""
    _rule(4, "Systems")

    scanned = scan_tree(roms)
    playable = [s for s in scanned if s.is_known and s.roms]
    if not playable:
        _note("[warn]No recognised systems with ROMs found.[/]")
        _note("[dim]`pyskraper systems` lists every folder and says which are unmapped.[/]")
        return []

    excluded = {name.lower() for name in config.systems.exclude}
    selected = {s.folder for s in playable if s.folder.lower() not in excluded}

    unmapped = [s for s in scanned if not s.is_known]
    _note(f"{len(playable)} system(s) with ROMs" + (f", {len(unmapped)} unrecognised folder(s)" if unmapped else ""))
    if excluded & {s.folder.lower() for s in playable}:
        _note("[dim]Pre-deselected the systems your config excludes.[/]")
    console.print()

    while True:
        _print_systems(playable, selected)
        console.print("\n   [dim]Enter to accept · numbers to toggle (e.g. 1,4,7) · [key]a[/] all · [key]n[/] none[/]")
        answer = Prompt.ask("   Toggle", default="", console=console).strip().lower()
        if not answer:
            break
        if answer == "a":
            selected = {s.folder for s in playable}
            continue
        if answer == "n":
            selected = set()
            continue
        if not _toggle(answer, playable, selected):
            console.print("   [danger]Enter numbers from the list, or a / n.[/]")

    config.systems.include = sorted(selected)
    # include= is now authoritative, so a stale exclude list would only be
    # confusing -- and could silently drop a system the user just ticked.
    config.systems.exclude = []
    chosen = [s for s in playable if s.folder in selected]
    total = sum(len(s.roms) for s in chosen)
    _note(f"[ok]{len(chosen)} system(s), {total:,} ROMs selected.[/]")
    return chosen


def _print_systems(playable: list[ScannedSystem], selected: set[str]) -> None:
    table = Table(show_header=True, header_style="label", box=None, pad_edge=False)
    table.add_column(" ", width=3)
    table.add_column("", width=3)
    table.add_column("System")
    table.add_column("ROMs", justify="right")
    for index, system in enumerate(playable, start=1):
        on = system.folder in selected
        table.add_row(
            "[marker]x[/]" if on else " ",
            str(index),
            system.folder if on else f"[dim]{system.folder}[/]",
            f"{len(system.roms):,}" if on else f"[dim]{len(system.roms):,}[/]",
        )
    console.print(table)


def _toggle(answer: str, playable: list[ScannedSystem], selected: set[str]) -> bool:
    """Apply a comma-separated list of indices.  False if none parsed."""
    hit = False
    for token in answer.replace(" ", ",").split(","):
        if not token:
            continue
        try:
            index = int(token)
        except ValueError:
            continue
        if not 1 <= index <= len(playable):
            continue
        folder = playable[index - 1].folder
        selected.symmetric_difference_update({folder})
        hit = True
    return hit


def step_media(config: Config) -> None:
    """Recommended, everything, or hand-picked."""
    _rule(5, "Media")

    recommended = RECOMMENDED_MEDIA
    included = ", ".join(sorted(tag for tag, keys in recommended.items() if keys))
    console.print(f"   [marker]>[/] [key]1[/]  Recommended ({included})")
    console.print("     [key]2[/]  Everything (every media type ScreenScraper offers)")
    console.print("     [key]3[/]  Custom\n")
    _note(f"[dim]{', '.join(_HEAVY_TAGS)} are by far the largest — they dominate download size.[/]\n")

    choice = _ask_index("   Which?", 3, default=1)
    if choice == 1 and recommended:
        config.media = dict(recommended)
    elif choice == 2:
        config.media = {tag: list(keys) for tag, keys in DEFAULT_MEDIA.items() if keys}
    elif choice == 3:
        _choose_media(config)

    enabled = config.enabled_tags()
    _note(f"[ok]{len(enabled)} media type(s):[/] {', '.join(sorted(enabled))}")


def _choose_media(config: Config) -> None:
    console.print()
    chosen: dict[str, list[str]] = {}
    for tag, keys in DEFAULT_MEDIA.items():
        if not keys:
            continue
        heavy = " [warn](large)[/]" if tag in _HEAVY_TAGS else ""
        if Confirm.ask(f"   {tag}{heavy}", default=bool(config.media.get(tag)), console=console):
            chosen[tag] = list(keys)
    config.media = chosen


def step_preferences(config: Config) -> None:
    """Language and region, which drive the whole media fallback chain."""
    _rule(6, "Language and region")
    prefs = config.preferences
    _note("[dim]Two-letter codes: en, fr, de, es, it, pt, ja …[/]")
    prefs.language = (
        Prompt.ask("   Preferred language", default=prefs.language, console=console) or prefs.language
    ).lower()
    _note("[dim]Region decides which box art and title screen you get: us, eu, jp, wor …[/]")
    prefs.region = (Prompt.ask("   Preferred region", default=prefs.region, console=console) or prefs.region).lower()


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------


def step_summary(config: Config, chosen: list[ScannedSystem], snapshot: QuotaSnapshot | None) -> None:
    _rule(7, "Ready to scrape")
    roms = config.paths.roms
    total = sum(len(s.roms) for s in chosen)

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("", style="label", width=14)
    table.add_column("")
    table.add_row("Card", str(roms))
    table.add_row("Systems", f"{len(chosen)} selected")
    table.add_row("ROMs", f"{total:,}")
    table.add_row("Media", ", ".join(sorted(config.enabled_tags())) or "none")
    table.add_row("Output", config.output.format)
    table.add_row("Language", f"{config.preferences.language} · region {config.preferences.region}")
    if roms is not None:
        table.add_row("Free space", format_bytes(free_bytes(roms)))
    if snapshot is not None and snapshot.max_requests_per_day:
        left = snapshot.max_requests_per_day - snapshot.requests_today
        table.add_row("Quota", f"{left:,} of {snapshot.max_requests_per_day:,} requests left today")
    console.print(table)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _ask_index(question: str, maximum: int, *, default: int) -> int:
    """A numbered choice.  Enter accepts the default; bad input re-asks."""
    while True:
        answer = Prompt.ask(question, default=str(default), console=console)
        try:
            value = int(str(answer).strip())
        except (TypeError, ValueError):
            console.print(f"   [danger]Enter a number from 1 to {maximum}.[/]")
            continue
        if 1 <= value <= maximum:
            return value
        console.print(f"   [danger]Enter a number from 1 to {maximum}.[/]")


# Wider than any panel shipped, but low enough that a typo like 64000x480 is
# caught rather than turned into a resize nobody wanted.
_MAX_DIMENSION = 8192


def _ask_resolution(question: str) -> tuple[int, int] | None:
    """A ``WIDTHxHEIGHT`` answer, or ``None`` for empty.  Bad input re-asks."""
    while True:
        # show_default=False: the default is "no cap", and Rich would render
        # that as a bare "()" that reads like a mistake.
        answer = str(Prompt.ask(question, default="", show_default=False, console=console) or "").strip().lower()
        if not answer:
            return None
        width, _, height = answer.partition("x")
        try:
            size = (int(width.strip()), int(height.strip()))
        except ValueError:
            console.print("   [danger]Enter it as WIDTHxHEIGHT, e.g. 640x480.[/]")
            continue
        if all(1 <= value <= _MAX_DIMENSION for value in size):
            return size
        console.print(f"   [danger]Both numbers must be between 1 and {_MAX_DIMENSION}.[/]")


def can_prompt() -> bool:
    """Whether there is a human here to answer.

    Checked rather than assumed: a wizard that blocks on a prompt inside a cron
    job hangs forever, and the symptom -- a job that never finishes -- gives no
    hint about the cause.
    """
    return sys.stdin is not None and sys.stdin.isatty()


def run_wizard(config_file: Path | None = None, *, verify: bool = True) -> int:
    """Walk through setup, then offer to start the scrape.

    Returns a process exit code.  Nothing is written until the final step, so
    abandoning the wizard at any point leaves the machine exactly as it was.
    """
    print_banner(console)
    console.print("[dim]Ctrl-C at any point exits without saving.[/]")

    try:
        config = load_config(config_path=config_file)
    except ConfigError as exc:
        console.print(f"[danger]Could not read the existing config:[/] {exc}")
        return EXIT_CONFIG

    try:
        snapshot = step_credentials(config, verify=verify)
        roms = step_roms(config)
        card = next((c for c in find_cards() if c.roms == roms), None)
        step_device(config, card)
        chosen = step_systems(config, roms)
        step_media(config)
        step_preferences(config)
        step_summary(config, chosen, snapshot)

        console.print(
            "\n   [key]Enter[/] save and start scraping · [key]s[/] save and exit · [key]q[/] quit without saving"
        )
        answer = (Prompt.ask("   ", default="", console=console) or "").strip().lower()
    except KeyboardInterrupt:
        console.print("\n[warn]Cancelled.[/] Nothing was saved.")
        return EXIT_INTERRUPTED
    except EOFError:
        # stdin ran out mid-run: a piped script that answered fewer questions
        # than were asked.  Same guarantee as Ctrl-C -- write nothing.
        console.print("\n[warn]Input ended before setup finished.[/] Nothing was saved.")
        return EXIT_INTERRUPTED

    if answer == "q":
        console.print("[warn]Quit.[/] Nothing was saved.")
        return EXIT_OK

    destination = config_file or config_path()
    saved = save_config(config, destination)
    console.print(f"\n[ok]Saved[/] {saved}")
    console.print("[dim]It contains your passwords and is readable only by you. Keep it out of version control.[/]")

    if answer == "s":
        console.print("Run [cmd]pyskraper scrape[/] when you are ready.")
        console.print("[dim]Want an idea of the size first? `pyskraper scrape --dry-run --limit 5`.[/]")
        return EXIT_OK

    return _start_scrape(saved)


def _start_scrape(config_file: Path) -> int:
    """Hand over to the real scrape command."""
    from .cli import scrape  # Imported here to keep the CLI's import graph acyclic.

    console.print("\n[title]Starting.[/] Ctrl-C stops; progress is journalled and resumes.\n")
    try:
        scrape(config_path=config_file)
    except SystemExit as exc:  # typer.Exit
        return int(exc.code or 0)
    return EXIT_OK
