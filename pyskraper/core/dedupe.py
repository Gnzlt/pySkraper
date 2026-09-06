"""Duplicate detection, and the safety discipline around acting on it.

Detection is nearly free: the hash index built during scanning already answers
"which files are byte-identical", and the ScreenScraper game IDs written during
a scrape already answer "which files are the same game".  Almost everything in
this module is therefore about the *other* half -- not deciding what is a
duplicate, but refusing to act on that decision until the user has asked twice.

Two ideas carry the safety:

**Conflicting keep rules skip the group.**  If the preferred-region copy is not
the latest revision, no rule breaks the tie by guessing.  The group is reported
and left alone.  A duplicate left in place costs a few megabytes; a wrongly
deleted ROM costs a file that may not be replaceable.

**Dry run and real run are the same code path.**  The plan is built in full --
every source path, every destination, every gamelist entry -- and only the last
few syscalls are gated on ``apply``.  A report that came from different code
than the action is a report that can lie.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .atomic import atomic_binary
from .cache import Cache
from .hasher import hash_file
from .models import EntryInfo
from .naming import FileTags, parse_tags
from .scanner import ScannedSystem

__all__ = [
    "Action",
    "ActionOutcome",
    "DedupeError",
    "DedupeReport",
    "DuplicateGroup",
    "DuplicateKind",
    "LibraryWriter",
    "PlannedRemoval",
    "RemovalPlan",
    "RomEntry",
    "Rule",
    "apply_plan",
    "build_entries",
    "find_duplicates",
    "plan_removals",
]

log = logging.getLogger(__name__)


class DedupeError(Exception):
    """A dedupe configuration problem the user can act on."""


class LibraryWriter(Protocol):
    """The slice of the ``Writer`` protocol hygiene needs.

    Declared structurally rather than imported so ``core`` never depends on
    ``output``: dedupe needs to *ask* a writer where the media is, not to know
    that writers exist.
    """

    def list_entries(self, system_dir: Path) -> list[EntryInfo]: ...

    def media_index(self, system_dir: Path) -> dict[Path, str]: ...

    def remove_entries(self, rom_paths: list[Path], system_dir: Path) -> int: ...

    def known_media_dirs(self) -> frozenset[str]: ...


class DuplicateKind(StrEnum):
    EXACT = "exact"
    """Byte-identical content under two paths."""
    SAME_GAME = "same-game"
    """Different dumps that ScreenScraper resolves to one game."""


class Action(StrEnum):
    REPORT_ONLY = "report-only"
    QUARANTINE = "quarantine"
    DELETE = "delete"


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RomEntry:
    """One ROM, with everything the keep rules need to judge it."""

    path: Path
    system: str
    system_dir: Path
    size: int
    md5: str | None = None
    game_id: int | None = None
    game_name: str | None = None
    in_gamelist: bool = False
    tags: FileTags = field(default_factory=FileTags)

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def name(self) -> str:
        return self.path.name


def build_entries(
    systems: Sequence[ScannedSystem],
    *,
    cache: Cache | None = None,
    writer: LibraryWriter | None = None,
    rehash: bool = False,
    on_progress: Callable[[Path], None] | None = None,
) -> list[RomEntry]:
    """Assemble the index dedupe reasons over.

    Hashes come from the cache when the file is unchanged, so a library scraped
    yesterday is indexed in seconds.  Game IDs come from the cache first and the
    front-end's metadata file second -- the cache is authoritative, but a
    library scraped before this cache existed still has its IDs on the card.
    """
    entries: list[RomEntry] = []

    for system in systems:
        if not system.is_known or system.info is None:
            continue

        listed: dict[Path, tuple[str | None, int | None]] = {}
        if writer is not None:
            for info in writer.list_entries(system.path):
                listed[info.rom_path] = (info.name, info.game_id)

        for rom_path in system.roms:
            if on_progress is not None:
                on_progress(rom_path)
            try:
                stat = rom_path.stat()
            except OSError as exc:
                log.warning("Could not stat %s: %s", rom_path, exc)
                continue

            md5: str | None = None
            hashes = None if rehash else (cache.get_hashes(rom_path, stat.st_size, stat.st_mtime) if cache else None)
            if hashes is None:
                try:
                    hashes = hash_file(rom_path)
                except OSError as exc:
                    log.warning("Could not hash %s: %s", rom_path, exc)
                if hashes is not None and cache is not None:
                    cache.put_hashes(rom_path, stat.st_size, stat.st_mtime, hashes)
            if hashes is not None and not hashes.truncated:
                md5 = hashes.md5

            name, game_id = listed.get(rom_path.resolve(), (None, None))
            if md5 and cache is not None:
                # No TTL here: a stale game ID is still the right game.  Expiring
                # it would silently turn same-game detection off on an old cache.
                cached = cache.get_game(md5, system.info.systeme_id, ttl=float("inf"))
                if cached is not None:
                    game_id = _int(cached.get("id")) or game_id

            entries.append(
                RomEntry(
                    path=rom_path,
                    system=system.folder,
                    system_dir=system.path,
                    size=stat.st_size,
                    md5=md5,
                    game_id=game_id,
                    game_name=name,
                    in_gamelist=rom_path.resolve() in listed,
                    tags=parse_tags(rom_path.name),
                )
            )

    return entries


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Keep rules
# --------------------------------------------------------------------------
#
# A rule scores each candidate; higher wins.  A rule that gives every candidate
# the same score has said nothing and is passed over.

KeepRule = Callable[[RomEntry], float]


@dataclass(frozen=True)
class Rule:
    """One keep rule, and whether it is allowed to signal a conflict.

    The distinction matters more than it looks.  A *substantive* rule states
    something about which release you want -- this is the USA copy, this is the
    later revision -- and can abstain when it has nothing to say.  A *tiebreak*
    rule has an opinion about every pair of files and therefore carries no such
    information: ``shortest-name`` prefers the shorter name whether or not that
    means anything.

    Only substantive rules can conflict.  Without that split, `shortest-name`
    would contradict `latest-revision` on every single group -- a revision
    marker always makes the filename longer -- and every duplicate in the
    library would be flagged as undecidable.
    """

    name: str
    score: KeepRule
    tiebreak: bool = False


def _region_rule(code: str) -> KeepRule:
    wanted = code.strip().lower()

    def score(entry: RomEntry) -> float:
        return 1.0 if wanted in entry.tags.regions else 0.0

    return score


_STATIC_RULES: dict[str, tuple[KeepRule, bool]] = {
    "verified": (lambda e: 1.0 if e.tags.verified else 0.0, False),
    "latest-revision": (lambda e: e.tags.revision, False),
    "in-gamelist": (lambda e: 1.0 if e.in_gamelist else 0.0, False),
    # Not in the default priority, but available: a `[b]` dump is a corrupt
    # file, and keeping it over a good one is the one outcome nobody wants.
    "good-dump": (lambda e: 0.0 if e.tags.bad_dump else 1.0, False),
    "original": (lambda e: 0.0 if e.tags.translated else 1.0, False),
    # Tiebreaks: always an opinion, never evidence.
    "shortest-name": (lambda e: -float(len(e.stem)), True),
    "largest": (lambda e: float(e.size), True),
}


def build_rules(priority: Sequence[str]) -> list[Rule]:
    """Resolve rule names into scoring functions, in order.

    An unknown name is an error rather than a shrug: silently ignoring a
    misspelled rule would change which file survives without saying so.
    """
    rules: list[Rule] = []
    for raw in priority:
        name = raw.strip()
        if not name:
            continue
        if name.startswith("region:"):
            rules.append(Rule(name, _region_rule(name.split(":", 1)[1])))
        elif name in _STATIC_RULES:
            score, tiebreak = _STATIC_RULES[name]
            rules.append(Rule(name, score, tiebreak))
        else:
            known = ", ".join(sorted(_STATIC_RULES)) + ", region:<code>"
            raise DedupeError(f"Unknown dedupe keep rule {name!r}. Available: {known}")
    return rules


def _verdict(rule: Rule, entries: Sequence[RomEntry]) -> RomEntry | None:
    """The single entry this rule prefers, or ``None`` if it cannot choose."""
    scores = [rule.score(entry) for entry in entries]
    best = max(scores)
    leaders = [entry for entry, score in zip(entries, scores, strict=True) if score == best]
    return leaders[0] if len(leaders) == 1 else None


def _choose_keeper(
    entries: Sequence[RomEntry], rules: Sequence[Rule], *, identical: bool
) -> tuple[RomEntry | None, str | None]:
    """Pick the survivor, or explain why no survivor can be picked safely.

    Substantive rules are consulted first and must agree.  Only if none of them
    has an opinion do the tiebreaks get a turn.  Returns ``(keeper, None)`` or
    ``(None, reason)`` -- the reason is shown to the user verbatim, so it names
    the rules that disagreed.
    """
    verdicts = [(rule.name, choice) for rule in rules if not rule.tiebreak if (choice := _verdict(rule, entries))]

    winners = {choice.path for _, choice in verdicts}
    if len(winners) > 1:
        detail = "; ".join(f"{name} wants {choice.name}" for name, choice in verdicts)
        return None, f"keep rules disagree ({detail})"
    if verdicts:
        return verdicts[0][1], None

    for rule in rules:
        if rule.tiebreak and (choice := _verdict(rule, entries)) is not None:
            return choice, None

    if identical:
        # Byte-identical files: whichever survives, the content is the same, so
        # a deterministic tie-break loses nothing.  Shallowest path first, so
        # the copy in the system root beats one buried in a subfolder.
        return min(entries, key=lambda e: (len(e.path.parts), str(e.path))), None
    return None, "no keep rule could tell these apart"


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


@dataclass
class DuplicateGroup:
    """One set of files that are the same thing."""

    kind: DuplicateKind
    key: str
    """The md5 (exact) or ScreenScraper game id (same-game) that binds them."""
    system: str
    entries: list[RomEntry]
    keeper: RomEntry | None = None
    skipped: str | None = None
    """Why nothing will be removed here, if that is the case."""
    cross_system: bool = False

    @property
    def actionable(self) -> bool:
        return self.keeper is not None and not self.cross_system and self.skipped is None

    @property
    def removals(self) -> list[RomEntry]:
        if not self.actionable:
            return []
        return [entry for entry in self.entries if entry.path != (self.keeper.path if self.keeper else None)]

    @property
    def label(self) -> str:
        if self.kind is DuplicateKind.EXACT:
            return f"identical content ({self.key[:8]})"
        name = next((e.game_name for e in self.entries if e.game_name), None)
        return f"same game #{self.key}" + (f" — {name}" if name else "")

    @property
    def reclaimable(self) -> int:
        return sum(entry.size for entry in self.removals)


@dataclass
class DedupeReport:
    groups: list[DuplicateGroup] = field(default_factory=list)

    @property
    def actionable(self) -> list[DuplicateGroup]:
        return [group for group in self.groups if group.actionable]

    @property
    def skipped(self) -> list[DuplicateGroup]:
        return [group for group in self.groups if group.skipped is not None]

    @property
    def cross_system(self) -> list[DuplicateGroup]:
        return [group for group in self.groups if group.cross_system]

    @property
    def removals(self) -> list[RomEntry]:
        return [entry for group in self.actionable for entry in group.removals]

    @property
    def reclaimable(self) -> int:
        return sum(group.reclaimable for group in self.actionable)


def find_duplicates(
    entries: Sequence[RomEntry],
    *,
    detect: Sequence[str] = ("exact", "same-game"),
    keep_priority: Sequence[str] = (),
) -> DedupeReport:
    """Group the index into duplicate sets and choose a survivor for each.

    Exact groups are resolved first and their losers are then invisible to
    same-game detection, so one file is never proposed for removal twice.
    """
    rules = build_rules(keep_priority)
    wanted = {item.strip().lower() for item in detect}
    report = DedupeReport()
    already_removing: set[Path] = set()

    if "exact" in wanted:
        for group in _group_exact(entries, rules):
            report.groups.append(group)
            already_removing.update(entry.path for entry in group.removals)

    if "same-game" in wanted:
        survivors = [entry for entry in entries if entry.path not in already_removing]
        report.groups.extend(_group_same_game(survivors, rules))

    report.groups.sort(key=lambda g: (g.cross_system, g.kind, g.system, g.key))
    return report


def _group_exact(entries: Sequence[RomEntry], rules: Sequence[Rule]) -> list[DuplicateGroup]:
    by_hash: dict[str, list[RomEntry]] = defaultdict(list)
    for entry in entries:
        if entry.md5:
            by_hash[entry.md5].append(entry)

    groups: list[DuplicateGroup] = []
    for md5, members in by_hash.items():
        if len(members) < 2:
            continue
        groups.extend(_partition(DuplicateKind.EXACT, md5, members, rules, identical=True))
    return groups


def _group_same_game(entries: Sequence[RomEntry], rules: Sequence[Rule]) -> list[DuplicateGroup]:
    by_game: dict[int, list[RomEntry]] = defaultdict(list)
    for entry in entries:
        if entry.game_id is not None:
            by_game[entry.game_id].append(entry)

    groups: list[DuplicateGroup] = []
    for game_id, members in by_game.items():
        if len(members) < 2:
            continue
        groups.extend(_partition(DuplicateKind.SAME_GAME, str(game_id), members, rules, identical=False))
    return groups


def _partition(
    kind: DuplicateKind,
    key: str,
    members: Sequence[RomEntry],
    rules: Sequence[Rule],
    *,
    identical: bool,
) -> list[DuplicateGroup]:
    """Split one duplicate set into per-system groups, plus a cross-system note.

    Grouping is per-system because "the same file in `snes` and in `sfc`" is
    usually a deliberate choice about which front-end menu a game appears in,
    not an accident -- so it is reported and never actioned.
    """
    by_system: dict[str, list[RomEntry]] = defaultdict(list)
    for entry in members:
        by_system[entry.system].append(entry)

    groups: list[DuplicateGroup] = []
    for system, in_system in sorted(by_system.items()):
        if len(in_system) < 2:
            continue
        ordered = sorted(in_system, key=lambda e: str(e.path))
        keeper, skipped = _choose_keeper(ordered, rules, identical=identical)
        groups.append(
            DuplicateGroup(kind=kind, key=key, system=system, entries=ordered, keeper=keeper, skipped=skipped)
        )

    if len(by_system) > 1:
        groups.append(
            DuplicateGroup(
                kind=kind,
                key=key,
                system=", ".join(sorted(by_system)),
                entries=sorted(members, key=lambda e: str(e.path)),
                cross_system=True,
                skipped=f"spans {len(by_system)} systems — reported only, never actioned",
            )
        )
    return groups


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class PlannedRemoval:
    """One ROM's removal, resolved down to individual file operations."""

    entry: RomEntry
    reason: str
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    """``(source, destination)`` for quarantine; destination is unused for delete."""

    @property
    def files(self) -> list[Path]:
        return [source for source, _ in self.moves]

    @property
    def total_bytes(self) -> int:
        total = 0
        for source in self.files:
            try:
                total += source.stat().st_size
            except OSError:
                continue
        return total


@dataclass
class RemovalPlan:
    action: Action
    removals: list[PlannedRemoval] = field(default_factory=list)
    entries_to_unlist: dict[Path, list[Path]] = field(default_factory=dict)
    """system directory -> ROM paths whose metadata entry goes with them."""

    @property
    def rom_count(self) -> int:
        return len(self.removals)

    @property
    def file_count(self) -> int:
        return sum(len(removal.moves) for removal in self.removals)

    @property
    def total_bytes(self) -> int:
        return sum(removal.total_bytes for removal in self.removals)


def plan_removals(
    report: DedupeReport,
    *,
    entries: Sequence[RomEntry],
    action: Action,
    quarantine_dir: Path,
    writer: LibraryWriter | None = None,
) -> RemovalPlan:
    """Turn a report into an exact list of file operations.

    Built in full whether or not it will be executed -- this is the single code
    path behind both the dry-run report and the real run, so the two cannot
    diverge.

    ``entries`` is the whole index, not just the duplicates: deciding whether a
    media file can safely go needs to know about every ROM that survives, not
    only the ones that happened to land in a duplicate group.
    """
    plan = RemovalPlan(action=action)
    if action is Action.REPORT_ONLY:
        return plan

    doomed = {entry.path for entry in report.removals}
    surviving_stems: dict[Path, set[str]] = defaultdict(set)
    for entry in entries:
        if entry.path not in doomed:
            surviving_stems[entry.system_dir].add(entry.stem)

    media_by_system: dict[Path, dict[Path, str]] = {}
    claimed: set[Path] = set()

    for group in report.actionable:
        for entry in group.removals:
            system_dir = entry.system_dir
            if writer is not None and system_dir not in media_by_system:
                media_by_system[system_dir] = writer.media_index(system_dir)

            files = [entry.path]
            files.extend(_media_for(entry, surviving_stems[system_dir], media_by_system.get(system_dir, {})))

            moves: list[tuple[Path, Path]] = []
            for source in files:
                destination = _quarantine_target(source, entry, quarantine_dir, claimed)
                claimed.add(destination)
                moves.append((source, destination))

            plan.removals.append(PlannedRemoval(entry=entry, reason=group.label, moves=moves))
            if entry.in_gamelist:
                # Only what is actually listed: reporting a metadata entry that
                # was never there would inflate the plan the user is agreeing to.
                plan.entries_to_unlist.setdefault(system_dir, []).append(entry.path)

    return plan


def _media_for(entry: RomEntry, surviving_stems: set[str], index: dict[Path, str]) -> list[Path]:
    """This ROM's media files -- but only the ones no surviving ROM shares.

    ``Game.zip`` and ``Game.sfc`` in one folder resolve to the same media stem,
    so removing one would otherwise strip the artwork off the other.
    """
    if not index:
        return []
    if entry.stem in surviving_stems:
        log.debug("Keeping media for %s: a surviving ROM shares its stem", entry.name)
        return []
    return sorted(path for path, stem in index.items() if stem == entry.stem)


def _quarantine_target(source: Path, entry: RomEntry, quarantine_dir: Path, claimed: set[Path]) -> Path:
    """Mirror the card's layout under the quarantine root, so restoring is a move.

    Never returns a path that already exists: an earlier quarantine run's files
    are not something to overwrite.
    """
    try:
        relative = source.relative_to(entry.system_dir)
    except ValueError:
        relative = Path(source.name)

    target = quarantine_dir / entry.system / relative
    if not target.exists() and target not in claimed:
        return target

    stem, suffix = target.stem, target.suffix
    for counter in range(1, 1000):
        candidate = target.with_name(f"{stem}~{counter}{suffix}")
        if not candidate.exists() and candidate not in claimed:
            return candidate
    raise DedupeError(f"Cannot find a free quarantine name for {source}")


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


@dataclass
class ActionOutcome:
    moved: int = 0
    deleted: int = 0
    entries_unlisted: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)
    journal: Path | None = None


def apply_plan(
    plan: RemovalPlan,
    *,
    apply: bool,
    journal_dir: Path,
    writer: LibraryWriter | None = None,
) -> ActionOutcome:
    """Execute the plan, or rehearse it exactly.

    With ``apply=False`` every path is still resolved and every check still
    runs; only the four calls that touch the filesystem are skipped.
    """
    outcome = ActionOutcome()
    if plan.action is Action.REPORT_ONLY or not plan.removals:
        return outcome

    handle = _ActionJournal(journal_dir, plan.action) if apply else None
    outcome.journal = handle.path if handle else None

    try:
        for removal in plan.removals:
            for source, destination in removal.moves:
                if not source.exists():
                    # Planned from a scan that is now stale.  Not an error worth
                    # failing the run over, but never silently.
                    outcome.errors.append(f"vanished before removal: {source}")
                    continue
                size = source.stat().st_size
                try:
                    if plan.action is Action.QUARANTINE:
                        if apply and handle is not None:
                            _move(source, destination)
                            handle.record("quarantine", source, destination, size, removal.reason)
                        outcome.moved += 1
                    else:
                        if apply and handle is not None:
                            # Journalled *before* the unlink: a record of a file
                            # that survived is recoverable, the reverse is not.
                            handle.record("delete", source, None, size, removal.reason)
                            source.unlink()
                        outcome.deleted += 1
                    outcome.bytes_freed += size
                except OSError as exc:
                    outcome.errors.append(f"{source}: {exc}")

        if writer is not None:
            for system_dir, rom_paths in plan.entries_to_unlist.items():
                if apply:
                    outcome.entries_unlisted += writer.remove_entries(rom_paths, system_dir)
                else:
                    outcome.entries_unlisted += len(rom_paths)
    finally:
        if handle is not None:
            handle.close()

    return outcome


def _move(source: Path, destination: Path) -> None:
    """Move a file across devices without ever risking the source.

    The card and the quarantine directory are different filesystems, so this is
    a copy followed by an unlink.  The copy goes to ``<dest>.part``, is flushed
    and size-checked, and only then replaces the destination -- and only then is
    the original removed.  A failure at any point leaves the source untouched.
    """
    expected = source.stat().st_size
    # atomic_binary owns the part file, the fsync, the replace and the unlink
    # on failure. The size check stays here and stays *inside* the context: a
    # short copy has to raise before the replace, so the destination is never
    # created and the source is never reached.
    with atomic_binary(destination) as dst:
        with open(source, "rb") as src:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        if dst.tell() != expected:
            raise OSError(f"short copy of {source}: {dst.tell()} of {expected} bytes")

    # After the replace rather than before it: a copystat failure now leaves a
    # complete destination and an intact source, which is the safe direction.
    shutil.copystat(source, destination)
    source.unlink()


class _ActionJournal:
    """Append-only record of every file this run moved or deleted.

    The point is reversibility: source and destination for each move, so a
    mistaken quarantine can be undone by reading the file back, and a mistaken
    delete can at least be named precisely.
    """

    def __init__(self, directory: Path, action: Action) -> None:
        directory = Path(directory).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"dedupe-{stamp}-{action.value}.jsonl"
        self._handle = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - closed in close()

    def record(self, action: str, source: Path, destination: Path | None, size: int, reason: str) -> None:
        self._handle.write(
            json.dumps(
                {
                    "at": time.time(),
                    "action": action,
                    "src": str(source),
                    "dst": str(destination) if destination else None,
                    "bytes": size,
                    "reason": reason,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        # Flushed per line: the journal is worthless if it loses the last
        # entries to a buffer when the run is interrupted.
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()
