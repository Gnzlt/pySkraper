"""The ``pyskraper`` command line."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.progress import (
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from . import __version__
from .api.client import ApiCredentials, ScreenScraperClient
from .api.errors import ScreenScraperError
from .api.quota import QuotaGovernor, QuotaSnapshot
from .banner import print_banner
from .config import Config, ConfigError, find_config_file, load_config
from .core.cache import Cache
from .core.dedupe import (
    Action,
    ActionOutcome,
    DedupeError,
    DedupeReport,
    RemovalPlan,
    apply_plan,
    build_entries,
    find_duplicates,
    plan_removals,
)
from .core.hasher import hash_file
from .core.journal import RunJournal, journal_path_for
from .core.scanner import ScannedSystem, scan_tree
from .core.scraper import ProgressEvent, RunStats, Scraper
from .core.verify import VerifyReport, clean_orphans, verify_library
from .detect import detect_profiles, read_board
from .devices import PROFILES, by_vendor
from .exits import EXIT_CONFIG, EXIT_OK, EXIT_PARTIAL
from .logging_setup import setup_logging
from .output import Writer, get_writer
from .paths import base_dir, boot_partition_beside, cache_path, is_writable, set_base_dir
from .systems import SYSTEMS, lookup
from .theme import SweepBarColumn
from .ui import format_bytes, free_bytes
from .wizard import can_prompt, run_wizard

app = typer.Typer(
    name="pyskraper",
    help="Scrape ScreenScraper.fr metadata and media onto a KNULLI/Batocera SD card.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _build_config(
    config_path: Path | None,
    *,
    roms: Path | None = None,
    device: str | None = None,
    jobs: int | None = None,
) -> Config:
    overrides: dict[str, Any] = {}
    if roms is not None:
        overrides["paths"] = {"roms": roms}
    if device is not None:
        overrides["device"] = device
    if jobs is not None:
        overrides["network"] = {"threads": jobs}
    return load_config(config_path=config_path, overrides=overrides)


def _fail(message: str, code: int = EXIT_CONFIG) -> None:
    err_console.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(code)


def _permission_hint(roms_root: Path, exc: PermissionError) -> str:
    return (
        f"Can't read {roms_root}: {exc.strerror or exc}.\n"
        "On macOS this is usually TCC blocking access to a mounted volume rather than a "
        "real permissions problem — grant your terminal app access under System Settings "
        "→ Privacy & Security → Files and Folders (or Network Volumes / Removable "
        "Volumes on newer macOS), then reopen the terminal and try again."
    )


def _scan_tree(roms_root: Path, *, include: list[str] | None, exclude: list[str] | None) -> list[ScannedSystem]:
    """`scan_tree`, with a plain-English error for the TCC/mount-permission case."""
    try:
        return scan_tree(roms_root, include=include, exclude=exclude)
    except PermissionError as exc:
        _fail(_permission_hint(roms_root, exc))
        raise typer.Exit(EXIT_CONFIG) from exc


def _credentials(config: Config) -> ApiCredentials:
    config.require_credentials()
    creds = config.screenscraper
    return ApiCredentials(
        devid=creds.devid,
        devpassword=creds.devpassword,
        ssid=creds.ssid,
        sspassword=creds.sspassword,
        softname=creds.softname,
    )


@dataclass
class CliState:
    """What the top-level callback hands down to every subcommand."""

    non_interactive: bool = False


def _state(ctx: typer.Context) -> CliState:
    """The callback's state, or a default when a command is invoked directly."""
    return ctx.obj if isinstance(ctx.obj, CliState) else CliState()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Where config, cache, logs and journals live. Defaults to this folder."),
    ] = None,
    non_interactive: Annotated[bool, typer.Option("--non-interactive", help="Never prompt. For cron and CI.")] = False,
) -> None:
    # Before anything else: every path lookup downstream reads this. Guarded,
    # because set_base_dir(None) *clears* an override rather than being a no-op,
    # and this callback runs on every invocation.
    if data_dir is not None:
        set_base_dir(data_dir)
    # Subcommands read this back off the context. Declaring it here and reading
    # it only here would mean `pyskraper --non-interactive dedupe --delete`
    # never reaches dedupe's refusal, which is the one guard that must hold.
    ctx.obj = CliState(non_interactive=non_interactive)
    if version:
        console.print(f"pyskraper {__version__}")
        raise typer.Exit(EXIT_OK)
    if ctx.invoked_subcommand is None:
        if non_interactive or not can_prompt():
            _print_commands()
            raise typer.Exit(EXIT_OK)
        raise typer.Exit(run_wizard())


def _print_commands() -> None:
    """What to run, for the cases where the wizard cannot: a pipe, cron, CI.

    Prompting with nobody there would hang forever, and a job that never
    finishes gives no hint why -- so the non-interactive path prints the map
    instead of asking for directions.
    """
    print_banner(console)
    console.print()
    console.print("Run [cyan]pyskraper[/] in a terminal for guided setup. Otherwise:\n")
    console.print("  [cyan]pyskraper setup[/]                      guided setup")
    console.print("  [cyan]pyskraper doctor[/]                     check credentials, card and quota")
    console.print("  [cyan]pyskraper systems[/]                    list systems found on the card")
    console.print("  [cyan]pyskraper devices[/]                    list supported devices and screen sizes")
    console.print("  [cyan]pyskraper scrape --system snes[/]       scrape one system")
    console.print("  [cyan]pyskraper dedupe[/]                     find duplicate ROMs (reports only)")
    console.print("  [cyan]pyskraper verify[/]                     check the library for drift and leftovers")
    console.print("  [cyan]pyskraper cache stats[/]                what previous runs remembered")
    console.print("  [cyan]pyskraper scrape --help[/]              every flag\n")


@app.command()
def setup(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file to write.")] = None,
) -> None:
    """Guided setup. The same thing `pyskraper` with no arguments does."""
    if not can_prompt():
        _fail("Guided setup needs a terminal. Edit the config file directly, or see `pyskraper scrape --help`.")
    raise typer.Exit(run_wizard(config_path))


@app.command()
def scrape(
    system: Annotated[list[str] | None, typer.Option("--system", "-s", help="Only these systems.")] = None,
    roms: Annotated[
        Path | None,
        typer.Option("--roms", help="ROM root, e.g. /Volumes/SHARE/roms or /run/media/you/SHARE/roms."),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Stop after N ROMs per system.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Resolve everything, write nothing.")] = False,
    jobs: Annotated[int | None, typer.Option("--jobs", "-j", help="Lower concurrency (never raises it).")] = None,
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file.")] = None,
    device: Annotated[
        str | None,
        typer.Option(
            "--device", help="Device profile, or `auto` to read it off the card. `pyskraper devices` lists them."
        ),
    ] = None,
    rehash: Annotated[bool, typer.Option("--rehash", help="Ignore cached hashes and recompute.")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass the local cache entirely.")] = False,
    no_hash: Annotated[
        bool, typer.Option("--no-hash", help="Skip content matching. Strictly worse: falls back to filenames.")
    ] = False,
    restart: Annotated[
        bool, typer.Option("--restart", help="Ignore resume state and scrape everything again.")
    ] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True, help="-v info, -vv debug.")] = 0,
) -> None:
    """Scrape metadata and media onto the card."""
    # The full-detail log always goes to the file, whatever -v says about the
    # console: the run that needs explaining is the one that already happened.
    setup_logging(verbose, log_file=base_dir() / "data" / "pyskraper.log")
    try:
        config = _build_config(config_path, roms=roms, device=device, jobs=jobs)
        if no_hash:
            config.identification.use_hash = False
        roms_root = config.require_roms()
        credentials = _credentials(config)
    except ConfigError as exc:
        _fail(str(exc))
        return

    include = list(system) if system else config.systems.include
    systems = _scan_tree(roms_root, include=include, exclude=config.systems.exclude)
    known = [s for s in systems if s.is_known and s.roms]
    if not known:
        _fail(f"No scrapeable systems found under {roms_root}. Try `pyskraper systems`.", EXIT_PARTIAL)
        return

    total = sum(min(len(s.roms), limit) if limit else len(s.roms) for s in known)
    library_total = sum(len(s.roms) for s in known)

    cache: Cache | None = None
    if not no_cache:
        cache = Cache(cache_path(config.paths.cache))
        if rehash:
            cache.clear(tables=("hashes",))

    # Resume is the default, not a flag you have to remember: a full library at
    # one thread runs for hours, and the things that interrupt it (quota, a
    # pulled card, a closed laptop) are routine rather than exceptional.
    journal = RunJournal(journal_path_for(config.paths.cache, roms_root))
    if restart:
        journal.clear()
    already_done = journal.load()
    if already_done and not dry_run:
        console.print(f"[cyan]Resuming[/] — {len(already_done):,} ROM(s) already done. Use --restart to redo them.")

    try:
        with journal:
            stats = asyncio.run(
                _run_scrape(
                    config, credentials, known, total, limit=limit, dry_run=dry_run, cache=cache, journal=journal
                )
            )
    finally:
        if cache is not None:
            cache.close()

    if dry_run:
        _print_cost_report(stats, library_total, roms_root)
    else:
        _print_summary(stats, dry_run=dry_run)
    raise typer.Exit(stats.exit_code)


def _print_cost_report(stats: RunStats, library_total: int, roms_root: Path) -> None:
    """What a real run would cost, extrapolated from the sample that was resolved.

    This exists because the failure it prevents is expensive and silent: with
    every media type enabled, a large library can easily project to more bytes
    than the card holds, and you only find out when the card fills up halfway
    through and the run dies with a half-scraped library.
    """
    sampled = max(1, stats.matched)
    bytes_per_game = stats.bytes_planned / sampled
    media_per_game = stats.media_planned / sampled

    table = Table(title="Cost estimate (dry run — nothing was written)")
    table.add_column("")
    table.add_column("Sampled", justify="right")
    table.add_column(f"Projected for {library_total:,} ROMs", justify="right")

    table.add_row("Games resolved", f"{stats.matched:,}", f"{library_total:,}")
    table.add_row(
        "Unmatched", f"{stats.unmatched:,}", f"~{round(stats.unmatched / max(1, stats.scanned) * library_total):,}"
    )
    table.add_row("Media files", f"{stats.media_planned:,}", f"~{round(media_per_game * library_total):,}")
    table.add_row(
        "Media size",
        f"{stats.bytes_planned / 1_048_576:.0f} MB",
        f"~{bytes_per_game * library_total / 1_073_741_824:.1f} GB",
    )
    console.print(table)

    if stats.bytes_by_tag:
        breakdown = Table(title="Where the bytes go")
        breakdown.add_column("Media tag")
        breakdown.add_column("Projected", justify="right")
        breakdown.add_column("Share", justify="right")
        total_bytes = sum(stats.bytes_by_tag.values()) or 1
        for tag, size in sorted(stats.bytes_by_tag.items(), key=lambda kv: -kv[1]):
            projected_tag = size / sampled * library_total
            breakdown.add_row(tag, f"{projected_tag / 1_073_741_824:.1f} GB", f"{100 * size / total_bytes:.0f}%")
        console.print(breakdown)

    projected = bytes_per_game * library_total
    free = free_bytes(roms_root)
    console.print(f"\nFree space on {roms_root}: [bold]{free / 1_073_741_824:.1f} GB[/]")

    if projected > free:
        console.print(
            f"[bold red]This will not fit.[/] Projected {projected / 1_073_741_824:.1f} GB "
            f"vs {free / 1_073_741_824:.1f} GB free.\n"
            "Trim `media:` in your config — `video` and `mix` are usually the bulk — "
            "or set `images.convert_to: jpg`."
        )
    elif projected > free * 0.8:
        console.print("[yellow]This will fit, but with little room to spare.[/]")
    else:
        console.print("[green]Fits comfortably.[/]")


async def _run_scrape(
    config: Config,
    credentials: ApiCredentials,
    systems: list[ScannedSystem],
    total: int,
    *,
    limit: int | None,
    dry_run: bool,
    cache: Cache | None = None,
    journal: RunJournal | None = None,
) -> RunStats:
    governor = QuotaGovernor(
        jobs_cap=config.network.jobs_cap,
        stop_at_quota_pct=config.network.stop_at_quota_pct,
    )
    writer = get_writer(
        config.output.format,
        merge=config.output.merge_gamelist,
        write_hashes=config.output.write_hashes,
        write_scraper_id=config.output.write_scraper_id,
    )

    async with ScreenScraperClient(
        credentials,
        governor,
        timeout=config.network.timeout,
        retries=config.network.retries,
    ) as client:
        try:
            snapshot = await client.user_info()
        except ScreenScraperError as exc:
            err_console.print(f"[bold red]{type(exc).__name__}:[/] {exc}")
            raise typer.Exit(EXIT_CONFIG) from exc

        who = snapshot.username or credentials.ssid
        if client.anonymous:
            console.print(
                f"[yellow]Anonymous mode[/] (developer credentials only) — "
                f"{governor.concurrency} thread, {snapshot.max_download_speed} kb/s downloads.\n"
                "Add your ScreenScraper account to `screenscraper.ssid`/`sspassword` for a much higher allowance."
            )
        else:
            console.print(f"Signed in as [bold]{who}[/] — {governor.concurrency} thread(s)")
        console.print(
            f"{snapshot.requests_today}/{snapshot.max_requests_per_day} requests used today, "
            f"KO budget {snapshot.requests_ko_today}/{snapshot.max_requests_ko_per_day}"
        )

        # A redrawing bar is unreadable once stdout is a file or a pipe, and
        # that is exactly when someone is keeping the output. Disabling it makes
        # rich fall through to plain prints, so the same code serves both.
        live = console.is_terminal
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            SweepBarColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[requests]} req[/]"),
            TimeElapsedColumn(),
            TextColumn("[dim]eta[/]"),
            TimeRemainingColumn(),
            console=console,
            transient=False,
            disable=not live,
        ) as progress:
            task = progress.add_task("Scraping", total=total, requests=0)
            seen_systems: set[str] = set()
            done = 0
            start = time.monotonic()

            def on_progress(event: ProgressEvent) -> None:
                nonlocal done
                done += 1

                if event.system not in seen_systems:
                    seen_systems.add(event.system)
                    count = next((len(s.roms) for s in systems if s.folder == event.system), 0)
                    progress.console.print(f"\n[bold]{event.system}[/] — {count:,} ROMs")

                label = event.rom.stem[:44]
                progress.update(
                    task,
                    advance=1,
                    description=f"{event.system}: {label} [{event.method}]",
                    requests=stats_requests(),
                )

                # Only the things worth reading afterwards get a line. A matched
                # ROM is the expected case and says nothing; an unmatched one is
                # a ROM whose filename needs looking at.
                if event.error:
                    progress.console.print(f"  [red]![/] {label}  [red]{event.error}[/]")
                elif event.result is None:
                    progress.console.print(f"  [yellow]-[/] {label}  [dim]no match[/]")
                elif not live and done % 100 == 0:
                    elapsed = time.monotonic() - start
                    remaining = (elapsed / done) * (total - done) if done else None
                    eta = f"  ~{timedelta(seconds=int(remaining))} left" if remaining else ""
                    progress.console.print(f"  [{done:,}/{total:,}] {event.system}{eta}")

            def stats_requests() -> int:
                return scraper.stats.api_requests

            scraper = Scraper(
                config,
                client,
                writer,
                dry_run=dry_run,
                limit=limit,
                on_progress=on_progress,
                cache=cache,
                journal=journal,
            )
            return await scraper.run(systems)


def _print_summary(stats: RunStats, *, dry_run: bool) -> None:
    table = Table(title="Run summary" + (" (dry run)" if dry_run else ""), show_header=False)
    table.add_row("Scanned", str(stats.scanned))
    if stats.resumed:
        table.add_row("Skipped (already done)", str(stats.resumed))
    table.add_row("Matched", str(stats.matched))
    table.add_row("Unmatched", str(stats.unmatched))
    table.add_row("Failed", str(stats.failed))
    table.add_row("Media downloaded", f"{stats.media_downloaded} ({stats.bytes_downloaded / 1_048_576:.1f} MB)")
    table.add_row("Media already present", str(stats.media_skipped))
    if stats.media_resized:
        table.add_row("Media resized", str(stats.media_resized))
    if stats.media_failed:
        table.add_row("Media failed", str(stats.media_failed))
    if stats.by_method:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(stats.by_method.items()))
        table.add_row("Resolved by", breakdown)
    console.print(table)

    if stats.stopped_early:
        err_console.print(
            "[yellow]Stopped early: quota threshold reached.[/] "
            "Run the same command tomorrow — completed ROMs are journalled and will be skipped."
        )


@app.command()
def systems(
    roms: Annotated[Path | None, typer.Option("--roms", help="ROM root.")] = None,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verify: Annotated[bool, typer.Option("--verify", help="Check systeme IDs against the live API.")] = False,
) -> None:
    """List systems on the card and how they map to ScreenScraper."""
    setup_logging(0)
    try:
        config = _build_config(config_path, roms=roms)
    except ConfigError as exc:
        _fail(str(exc))
        return

    if verify:
        asyncio.run(_verify_systems(config))
        return

    if config.paths.roms is None or not config.paths.roms.exists():
        table = Table(title="Known systems (no ROM path configured)")
        table.add_column("Folder")
        table.add_column("ID", justify="right")
        table.add_column("Label")
        for info in SYSTEMS:
            table.add_row(info.folder, str(info.systeme_id), info.label)
        console.print(table)
        return

    scanned = _scan_tree(config.paths.roms, include=config.systems.include, exclude=config.systems.exclude)
    table = Table(title=f"Systems under {config.paths.roms}")
    table.add_column("Folder")
    table.add_column("ROMs", justify="right")
    table.add_column("ID", justify="right")
    table.add_column("Label")
    for entry in scanned:
        # `is_known` is exactly "info is not None", but only the narrowing on
        # the attribute itself convinces the type checker -- and unlike an
        # assert this survives `python -O`.
        if entry.info is not None:
            table.add_row(entry.folder, str(len(entry.roms)), str(entry.systeme_id), entry.info.label)
        else:
            table.add_row(f"[yellow]{entry.folder}[/]", "-", "-", "[yellow]unmapped[/]")
    console.print(table)


async def _verify_systems(config: Config) -> None:
    """Cross-check the committed table against ``systemesListe.php``.

    The table is hand-curated, so this is the command that turns "probably
    right" into "checked".
    """
    credentials = _credentials(config)
    governor = QuotaGovernor(stop_at_quota_pct=config.network.stop_at_quota_pct)
    async with ScreenScraperClient(credentials, governor, timeout=config.network.timeout) as client:
        remote = await client.systems_list()

    by_id: dict[int, str] = {}
    for entry in remote:
        try:
            ident = int(str(entry.get("id")))
        except (TypeError, ValueError):
            continue
        names = entry.get("noms")
        label = ""
        if isinstance(names, dict):
            label = str(names.get("nom_eu") or names.get("noms_commun") or names.get("nom_us") or "")
        by_id[ident] = label

    table = Table(title="System table verification")
    table.add_column("Folder")
    table.add_column("ID", justify="right")
    table.add_column("Local label")
    table.add_column("ScreenScraper says")
    table.add_column("")

    problems = 0
    for info in SYSTEMS:
        remote_label = by_id.get(info.systeme_id)
        if remote_label is None:
            problems += 1
            table.add_row(info.folder, str(info.systeme_id), info.label, "[red]no such id[/]", "✗")
        else:
            table.add_row(info.folder, str(info.systeme_id), info.label, remote_label, "")
    console.print(table)
    console.print(f"{len(SYSTEMS)} entries checked, {problems} unknown id(s).")


@app.command()
def quota(
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Show the live API allowance for your account."""
    setup_logging(0)
    try:
        config = _build_config(config_path)
        credentials = _credentials(config)
    except ConfigError as exc:
        _fail(str(exc))
        return

    async def go() -> None:
        governor = QuotaGovernor(stop_at_quota_pct=config.network.stop_at_quota_pct)
        async with ScreenScraperClient(credentials, governor, timeout=config.network.timeout) as client:
            snapshot = await client.user_info()

        table = Table(title="ScreenScraper allowance", show_header=False)
        table.add_row("User", snapshot.username or credentials.ssid)
        table.add_row("Level", str(snapshot.level))
        table.add_row("Threads", str(snapshot.max_threads))
        table.add_row("Requests / minute", str(snapshot.max_requests_per_min))
        table.add_row("Requests today", f"{snapshot.requests_today} / {snapshot.max_requests_per_day}")
        table.add_row(
            "Not-found (KO) today",
            f"{snapshot.requests_ko_today} / {snapshot.max_requests_ko_per_day}"
            "   [dim](the smaller budget — spent by ROMs that match nothing)[/]",
        )
        table.add_row("Max download speed", f"{snapshot.max_download_speed} kb/s")
        console.print(table)

    try:
        asyncio.run(go())
    except ScreenScraperError as exc:
        _fail(f"{type(exc).__name__}: {exc}")


@app.command()
def doctor(
    roms: Annotated[Path | None, typer.Option("--roms", help="ROM root.")] = None,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Check the configuration, the card and the credentials before a real run."""
    setup_logging(0)
    ok = True

    root = base_dir()
    console.print(f"Base directory: {root}")
    source = find_config_file(config_path)
    console.print(f"Config file: {source or '[yellow]none found (using defaults)[/]'}")
    if not is_writable(root):
        console.print("[red]x[/] Base directory is not writable — config and cache cannot be saved")
        ok = False

    try:
        config = _build_config(config_path, roms=roms)
    except ConfigError as exc:
        _fail(str(exc))
        return

    profile = PROFILES.get(config.device or "none")
    label = f"{profile.vendor} {profile.name}".strip() if profile else str(config.device)
    console.print(f"Device profile: {label}")

    # Printed whether or not it agrees with the profile: a card that says one
    # thing while the config says another is exactly what doctor is for.
    if config.paths.roms is not None:
        volume = config.paths.roms.parent
        boot = boot_partition_beside(volume)
        board = read_board(volume, boot)
        if board is not None:
            named = ", ".join(f"{p.vendor} {p.name}".strip() for p in detect_profiles(volume, boot))
            says = f" — {named}" if named else " — no profile for it yet"
            console.print(f"Card reports:   board `{board}`{says}")

    missing = config.screenscraper.missing()
    if missing:
        console.print(f"[red]✗[/] Developer credentials missing: {', '.join(missing)}")
        ok = False
    elif config.screenscraper.has_user_account:
        console.print("[green]✓[/] Developer and member credentials present")
    else:
        console.print(
            "[yellow]![/] Developer credentials only — anonymous mode works but is limited "
            "to 1 thread and slow downloads"
        )

    if " " in config.screenscraper.softname:
        console.print("[red]✗[/] softname contains a space — this corrupts returned media URLs")
        ok = False

    roms_root = config.paths.roms
    if roms_root is None:
        console.print("[red]✗[/] No ROM path configured")
        ok = False
    elif not roms_root.exists():
        console.print(f"[red]✗[/] ROM path does not exist: {roms_root}")
        ok = False
    else:
        writable = is_writable(roms_root)
        console.print(f"[green]✓[/] ROM path: {roms_root}")
        if not writable:
            console.print("[red]✗[/] ROM path is not writable — the card may be mounted read-only")
            ok = False
        try:
            scanned = scan_tree(roms_root, include=config.systems.include, exclude=config.systems.exclude)
        except PermissionError as exc:
            console.print(f"[red]✗[/] {_permission_hint(roms_root, exc)}")
            scanned = []
            ok = False
        mapped = [s for s in scanned if s.is_known]
        unmapped = [s for s in scanned if not s.is_known]
        if scanned:
            console.print(f"    {len(mapped)} mapped system(s), {sum(len(s.roms) for s in mapped)} ROMs")
        if unmapped:
            # A stock card has ~120 of these (ports, engines, homebrew loaders).
            # Printing them all buries everything else, so show a sample.
            names = [s.folder for s in unmapped]
            shown = ", ".join(names[:8])
            more = f" (+{len(names) - 8} more — see `pyskraper systems`)" if len(names) > 8 else ""
            console.print(f"    [yellow]{len(names)} unmapped folder(s):[/] {shown}{more}")

    if not missing:
        try:
            asyncio.run(_check_api(config))
            console.print("[green]✓[/] API credentials verified")
        except ScreenScraperError as exc:
            console.print(f"[red]✗[/] API check failed: {type(exc).__name__}: {exc}")
            ok = False

    raise typer.Exit(EXIT_OK if ok else EXIT_CONFIG)


async def _check_api(config: Config) -> QuotaSnapshot:
    """Verify the credentials and report the allowance they bought.

    Returns the snapshot rather than discarding it so the wizard can show real
    thread and quota numbers from the same call that proves the login works --
    one round trip, one source of truth.
    """
    credentials = _credentials(config)
    governor = QuotaGovernor(stop_at_quota_pct=config.network.stop_at_quota_pct)
    async with ScreenScraperClient(credentials, governor, timeout=config.network.timeout) as client:
        return await client.user_info()


@app.command(name="hash")
def hash_command(
    file: Annotated[Path, typer.Argument(help="ROM file to hash.")],
    system: Annotated[str | None, typer.Option("--system", "-s", help="System folder name.")] = None,
) -> None:
    """Print a file's hashes and the exact lookup we would send for it."""
    if not file.exists():
        _fail(f"No such file: {file}")
        return

    hashes = hash_file(file)
    info = lookup(system) if system else lookup(file.parent.name)

    table = Table(title=file.name, show_header=False)
    table.add_row("Size", f"{hashes.size:,} bytes")
    table.add_row("CRC32", hashes.crc32)
    table.add_row("MD5", hashes.md5)
    table.add_row("SHA1", hashes.sha1)
    table.add_row("System", f"{info.label} (id {info.systeme_id})" if info else "[yellow]unknown[/]")
    console.print(table)

    params = {
        "systemeid": info.systeme_id if info else None,
        "romtype": "rom",
        "romnom": file.name,
        "romtaille": hashes.size,
        **hashes.as_lookup(),
    }
    console.print("\n[dim]jeuInfos.php parameters:[/]")
    for key, value in params.items():
        if value is not None:
            console.print(f"  {key} = {value}")


cache_app = typer.Typer(help="Inspect and clear the local cache.")
app.add_typer(cache_app, name="cache")


@cache_app.command("stats")
def cache_stats(config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None) -> None:
    """Show what the cache is holding."""
    config = _build_config(config_path)
    with Cache(cache_path(config.paths.cache)) as cache:
        stats = cache.stats()

    table = Table(title="Cache", show_header=False)
    table.add_row("Location", str(cache_path(config.paths.cache)))
    table.add_row("Size", f"{stats.size_bytes / 1_048_576:.1f} MB")
    table.add_row("Hashed files", str(stats.hashes))
    table.add_row("Games", str(stats.games))
    table.add_row(
        "Remembered misses", f"{stats.misses}   [dim](ROMs that matched nothing — these protect the KO quota)[/]"
    )
    console.print(table)


@cache_app.command("clear")
def cache_clear(
    system: Annotated[str | None, typer.Option("--system", "-s", help="Only this system.")] = None,
    hashes: Annotated[bool, typer.Option("--hashes", help="Also drop cached hashes (forces a re-hash).")] = False,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Clear cached API responses. Never touches ROMs or media on the card."""
    config = _build_config(config_path)
    systeme_id = None
    if system:
        info = lookup(system)
        if info is None:
            _fail(f"Unknown system {system!r}")
            return
        systeme_id = info.systeme_id

    tables = ("games", "media", "misses", "hashes") if hashes else ("games", "media", "misses")
    with Cache(cache_path(config.paths.cache)) as cache:
        cache.clear(systeme_id=systeme_id, tables=tables)
    console.print("[green]✓[/] Cache cleared" + (f" for {system}" if system else ""))


@app.command()
def devices() -> None:
    """List the available device profiles."""
    table = Table(title="Device profiles")
    table.add_column("Vendor")
    table.add_column("Model")
    table.add_column("Id")
    table.add_column("Screen")
    table.add_column("Notes")
    for vendor, profiles in by_vendor().items():
        for index, profile in enumerate(profiles):
            screen = f"{profile.screen[0]}x{profile.screen[1]}" if profile.screen else "-"
            # The vendor is named once per group, and a rule closes the group so
            # the blank cells below it do not read as more of the same brand.
            table.add_row(
                vendor if index == 0 else "",
                profile.name,
                profile.id,
                screen,
                profile.notes,
                end_section=index == len(profiles) - 1,
            )
    none = PROFILES["none"]
    table.add_row("[dim]any[/]", none.name, none.id, "-", none.notes)
    console.print(table)
    console.print("\n[dim]Not listed? Use `none` and set images.max_width / max_height yourself.[/]")


# --------------------------------------------------------------------------
# Library hygiene
# --------------------------------------------------------------------------


def _hygiene_setup(
    config_path: Path | None,
    roms: Path | None,
    system: list[str] | None,
) -> tuple[Config, Path, list[ScannedSystem], Writer]:
    """Shared front half of `dedupe` and `verify`: config, scan, writer."""
    try:
        config = _build_config(config_path, roms=roms)
        roms_root = config.require_roms()
    except ConfigError as exc:
        _fail(str(exc))
        raise typer.Exit(EXIT_CONFIG) from exc

    include = list(system) if system else config.systems.include
    scanned = _scan_tree(roms_root, include=include, exclude=config.systems.exclude)
    known = [entry for entry in scanned if entry.is_known and entry.roms]
    if not known:
        _fail(f"No known systems with ROMs under {roms_root}. Try `pyskraper systems`.", EXIT_PARTIAL)

    writer = get_writer(config.output.format)
    return config, roms_root, known, writer


def _index_progress() -> Progress:
    """A bar whose label comes from the task, like the scrape progress does.

    It used to bake the label into the column and take a `total` it ignored,
    so the description handed to `add_task` never rendered and a caller could
    not change the wording mid-run.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        SweepBarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


@app.command()
def dedupe(
    ctx: typer.Context,
    system: Annotated[list[str] | None, typer.Option("--system", "-s", help="Only these systems.")] = None,
    roms: Annotated[Path | None, typer.Option("--roms", help="ROM root.")] = None,
    apply_changes: Annotated[
        bool,
        typer.Option("--apply", help="Delete the duplicates, after a confirmation. Without this, nothing changes."),
    ] = False,
    non_interactive: Annotated[
        bool, typer.Option("--non-interactive", help="Never prompt. Refuses to combine with --apply.")
    ] = False,
    detect: Annotated[
        list[str] | None, typer.Option("--detect", help="`exact`, `same-game`, or both (the default).")
    ] = None,
    show_all: Annotated[bool, typer.Option("--all", help="List every group, not just the first 20.")] = False,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
) -> None:
    """Find duplicate ROMs. Reports only, unless you ask twice.

    Detection is free — it reads the hash index and the game IDs a scrape
    already wrote. Acting on it is what needs care, so the default is to change
    nothing at all.
    """
    setup_logging(verbose)

    # Either spelling counts. `--non-interactive` reads naturally before the
    # subcommand as well as after it, and a guard a user can walk past by
    # moving a flag one word to the left is not a guard.
    unattended = non_interactive or _state(ctx).non_interactive

    # Refused before any work: an unattended irreversible delete is not a
    # feature, and there is no flag that makes it one.
    if apply_changes and unattended:
        _fail(
            "--apply cannot be combined with --non-interactive.\n"
            "Deleting ROMs unattended is refused outright. Run it without --non-interactive "
            "and confirm at the prompt, or drop --apply for a report.",
            EXIT_CONFIG,
        )

    config, _roms_root, known, writer = _hygiene_setup(config_path, roms, system)
    action = Action(config.dedupe.action)
    if not apply_changes:
        console.print("[dim]Report only — nothing will be deleted. Add --apply to act.[/]")

    total_roms = sum(len(entry.roms) for entry in known)
    cache = Cache(cache_path(config.paths.cache))
    try:
        with _index_progress() as progress:
            task = progress.add_task("Indexing", total=total_roms)
            entries = build_entries(
                known,
                cache=cache,
                writer=writer,
                on_progress=lambda _path: progress.update(task, advance=1),
            )

        try:
            report = find_duplicates(
                entries,
                detect=detect or config.dedupe.detect,
                keep_priority=config.dedupe.keep_priority,
            )
        except DedupeError as exc:
            _fail(str(exc))
            return

        _print_dedupe_report(report, show_all=show_all)

        if not report.actionable:
            console.print("\n[green]Nothing to do.[/]")
            raise typer.Exit(EXIT_OK)

        plan = plan_removals(report, entries=entries, action=action, writer=writer)

        if action is Action.REPORT_ONLY:
            console.print("\n[dim]dedupe.action is `report-only` — set it to `delete` to act.[/]")
            raise typer.Exit(EXIT_OK)

        _print_removal_plan(plan)

        if not apply_changes:
            console.print("\n[bold]Nothing was changed.[/] Re-run with [cyan]--apply[/] to delete these files.")
            raise typer.Exit(EXIT_OK)

        if not _confirm_delete(plan):
            console.print("[yellow]Cancelled.[/] Nothing was changed.")
            raise typer.Exit(EXIT_OK)

        with _index_progress() as progress:
            task = progress.add_task("Deleting", total=plan.file_count)
            outcome = apply_plan(
                plan,
                apply=True,
                journal_dir=config.paths.cache / "dedupe",
                writer=writer,
                on_progress=lambda _path: progress.update(task, advance=1),
            )
        _print_outcome(outcome)
        raise typer.Exit(EXIT_PARTIAL if outcome.errors else EXIT_OK)
    finally:
        cache.close()


def _confirm_delete(plan: RemovalPlan) -> bool:
    """The second ask. Shows what goes and requires the word, not a keypress."""
    console.print(
        f"\n[bold red]This will permanently delete {plan.file_count} file(s), "
        f"{format_bytes(plan.total_bytes)}, including {plan.rom_count} ROM(s).[/]"
    )
    console.print("[bold red]Removable media has no Trash. This cannot be undone.[/]")
    console.print("[dim]Every deleted path is written to a journal first, so you can see what went.[/]")
    answer: str = typer.prompt("Type 'delete' to confirm", default="", show_default=False)
    return answer.strip().lower() == "delete"


def _print_dedupe_report(report: DedupeReport, *, show_all: bool) -> None:
    groups = report.groups
    if not groups:
        console.print("[green]No duplicates found.[/]")
        return

    shown = groups if show_all else groups[:20]
    for group in shown:
        header = f"[bold]{group.system}[/] — {group.label}"
        if group.cross_system:
            header += "  [yellow](cross-system)[/]"
        console.print(f"\n{header}")
        for entry in group.entries:
            if group.keeper is not None and entry.path == group.keeper.path:
                marker, style = "keep", "green"
            elif group.actionable:
                marker, style = "remove", "red"
            else:
                marker, style = "—", "dim"
            console.print(f"  [{style}]{marker:>6}[/]  {entry.name}  [dim]({format_bytes(entry.size)})[/]")
        if group.skipped:
            console.print(f"  [yellow]skipped:[/] {group.skipped}")

    if len(groups) > len(shown):
        console.print(f"\n[dim]… and {len(groups) - len(shown)} more group(s). Use --all to see them.[/]")

    summary = Table(title="Duplicates", show_header=False)
    summary.add_row("Groups found", str(len(groups)))
    summary.add_row("Actionable", str(len(report.actionable)))
    if report.skipped:
        summary.add_row("Skipped", f"{len(report.skipped)}   [dim](rules disagreed — left alone on purpose)[/]")
    if report.cross_system:
        summary.add_row("Cross-system", f"{len(report.cross_system)}   [dim](reported, never actioned)[/]")
    summary.add_row("ROMs removable", str(len(report.removals)))
    summary.add_row("Space reclaimable", format_bytes(report.reclaimable))
    console.print()
    console.print(summary)


def _print_removal_plan(plan: RemovalPlan) -> None:
    table = Table(title="Delete plan", show_header=False)
    table.add_row("ROMs", str(plan.rom_count))
    table.add_row("Files in total", f"{plan.file_count}   [dim](ROMs plus their media)[/]")
    table.add_row("Metadata entries", str(sum(len(paths) for paths in plan.entries_to_unlist.values())))
    table.add_row("Size", format_bytes(plan.total_bytes))
    console.print()
    console.print(table)


def _print_outcome(outcome: ActionOutcome) -> None:
    table = Table(title="Done", show_header=False)
    table.add_row("Deleted", str(outcome.deleted))
    table.add_row("Metadata entries removed", str(outcome.entries_unlisted))
    table.add_row("Space reclaimed", format_bytes(outcome.bytes_freed))
    if outcome.journal:
        table.add_row("Journal", f"{outcome.journal}   [dim](every path this run removed)[/]")
    console.print(table)

    for error in outcome.errors:
        err_console.print(f"[yellow]![/] {error}")


@app.command()
def verify(
    ctx: typer.Context,
    system: Annotated[list[str] | None, typer.Option("--system", "-s", help="Only these systems.")] = None,
    roms: Annotated[Path | None, typer.Option("--roms", help="ROM root.")] = None,
    clean_orphans_flag: Annotated[
        bool, typer.Option("--clean-orphans", help="Remove media and metadata entries whose ROM is gone.")
    ] = False,
    apply_changes: Annotated[
        bool, typer.Option("--apply", help="Actually clean, after a confirmation. Without this, nothing changes.")
    ] = False,
    non_interactive: Annotated[
        bool, typer.Option("--non-interactive", help="Skip the confirmation prompt and clean straight away.")
    ] = False,
    no_rehash: Annotated[
        bool, typer.Option("--no-rehash", help="Skip content re-hashing (much faster, finds no drift).")
    ] = False,
    show_all: Annotated[bool, typer.Option("--all", help="List every affected file.")] = False,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
) -> None:
    """Check the library for drift and leftovers.

    Re-hashes every ROM and compares against the index, then cross-checks the
    front-end's metadata file against what is actually on the card.
    """
    setup_logging(verbose)
    config, _roms_root, known, writer = _hygiene_setup(config_path, roms, system)

    total_roms = sum(len(entry.roms) for entry in known)
    cache = Cache(cache_path(config.paths.cache))
    try:
        with _index_progress() as progress:
            task = progress.add_task("Verifying", total=total_roms)
            report = verify_library(
                known,
                cache=cache,
                writer=writer,
                rehash=not no_rehash,
                on_progress=lambda _path: progress.update(task, advance=1),
            )
    finally:
        cache.close()

    _print_verify_report(report, show_all=show_all, rehashed=not no_rehash)

    if not clean_orphans_flag:
        if report.orphan_media or report.missing_roms:
            console.print("\n[dim]Add --clean-orphans to remove the leftovers (then --apply to do it).[/]")
        raise typer.Exit(EXIT_OK if report.clean else EXIT_PARTIAL)

    if not apply_changes:
        media, entries, _errors = clean_orphans(report, apply=False, writer=writer)
        console.print(
            f"\n[bold]Would remove[/] {media} orphan media file(s) and {entries} metadata entry/entries "
            f"({format_bytes(report.orphan_bytes)}). Nothing was changed — add --apply."
        )
        raise typer.Exit(EXIT_OK)

    # The dry run is the thing being agreed to, so it runs first and its own
    # numbers -- not the report's -- are what the prompt quotes.
    media, entries, _errors = clean_orphans(report, apply=False, writer=writer)
    if media == 0 and entries == 0:
        console.print("\n[green]Nothing to clean.[/]")
        raise typer.Exit(EXIT_OK)

    unattended = non_interactive or _state(ctx).non_interactive
    if not unattended and not _confirm_clean(media, entries, report.orphan_bytes):
        console.print("[yellow]Cancelled.[/] Nothing was changed.")
        raise typer.Exit(EXIT_OK)

    with _index_progress() as progress:
        task = progress.add_task("Cleaning", total=media)
        media, entries, errors = clean_orphans(
            report,
            apply=True,
            writer=writer,
            on_progress=lambda _path: progress.update(task, advance=1),
        )
    console.print(f"\n[green]✓[/] Removed {media} orphan media file(s) and {entries} metadata entry/entries.")
    for error in errors:
        err_console.print(f"[yellow]![/] {error}")
    raise typer.Exit(EXIT_PARTIAL if errors else EXIT_OK)


def _confirm_clean(media: int, entries: int, orphan_bytes: int) -> bool:
    """A plain yes/no, not the word `delete` that `dedupe` demands.

    The ceremony is scaled to what is at stake. Nothing here is a ROM: orphan
    media and dead metadata entries both come back from a re-scrape, so the ask
    is for attention, not for a second thought.
    """
    console.print(
        f"\n[bold]About to remove {media} orphan media file(s) ({format_bytes(orphan_bytes)}) "
        f"and {entries} metadata entry/entries.[/]"
    )
    console.print("[dim]No ROM is touched. Everything removed here is restored by re-running `pyskraper scrape`.[/]")
    return typer.confirm("Proceed?", default=False)


def _print_verify_report(report: VerifyReport, *, show_all: bool, rehashed: bool) -> None:
    table = Table(title="Verify")
    table.add_column("System")
    table.add_column("ROMs", justify="right")
    table.add_column("Drifted", justify="right")
    table.add_column("Missing", justify="right")
    table.add_column("Orphan media", justify="right")
    table.add_column("Not scraped", justify="right")

    for entry in report.systems:
        drift = f"[red]{len(entry.drifted)}[/]" if entry.drifted else "0"
        missing = f"[yellow]{len(entry.missing_roms)}[/]" if entry.missing_roms else "0"
        orphan = f"[yellow]{len(entry.orphan_media)}[/]" if entry.orphan_media else "0"
        table.add_row(entry.system, str(entry.roms), drift, missing, orphan, str(len(entry.unlisted_roms)))
    console.print(table)

    if not rehashed:
        console.print("[dim]--no-rehash: content drift was not checked.[/]")

    limit = None if show_all else 10
    for entry in report.systems:
        if entry.drifted:
            console.print(f"\n[bold red]Content changed[/] in {entry.system}:")
            for path, before, after in entry.drifted[:limit]:
                console.print(f"  {path.name}  [dim]{before[:8]} → {after[:8]}[/]")
        if entry.missing_roms:
            console.print(f"\n[yellow]Listed but missing[/] in {entry.system}:")
            for path in entry.missing_roms[:limit]:
                console.print(f"  {path.name}")
        if entry.unreadable:
            console.print(f"\n[red]Unreadable[/] in {entry.system}:")
            for path, reason in entry.unreadable[:limit]:
                console.print(f"  {path.name}: {reason}")

    if report.drifted:
        console.print(
            "\n[dim]Drift is reported only. A changed ROM may be a better dump or a corrupt file — "
            "which of those it is, is not this tool's call.[/]"
        )


def run() -> None:
    logging.captureWarnings(True)
    app()


if __name__ == "__main__":
    run()
