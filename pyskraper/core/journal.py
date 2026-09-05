"""Run journal: append-only record of what has already been done.

A full library at one thread is an hours-long run, and the things that end it
early -- quota exhaustion, a pulled card, a closed laptop -- are all normal
rather than exceptional. The journal makes an interrupted run cost nothing:
work already completed is skipped on the next attempt.

Append-only JSONL, flushed per line. That is deliberate over anything cleverer:
a torn write costs at most the final line, which is re-done rather than lost,
and the file stays readable with `tail` while a run is in progress.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import TextIO

__all__ = ["RunJournal", "journal_path_for"]

log = logging.getLogger(__name__)


def journal_path_for(cache_dir: Path, roms_root: Path) -> Path:
    """One journal per ROM library, so two cards do not share resume state."""
    digest = hashlib.sha1(str(roms_root.resolve()).encode("utf-8")).hexdigest()[:12]
    return Path(cache_dir).expanduser() / "journals" / f"{digest}.jsonl"


class RunJournal:
    """Tracks completed ROMs across interrupted runs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._done: set[str] = set()
        self._handle: TextIO | None = None

    def load(self) -> set[str]:
        """Read the ROM paths already completed. Tolerates a torn final line."""
        if not self.path.exists():
            return set()
        done: set[str] = set()
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # The last line of an interrupted run may be half-written.
                    # Losing one entry means re-doing one ROM, which is fine.
                    log.debug("Ignoring incomplete journal line")
                    continue
                path = record.get("path")
                if isinstance(path, str):
                    done.add(path)
        self._done = done
        return done

    def __contains__(self, path: Path | str) -> bool:
        """Resume reads this: `scraper.py` filters the ROM list through it."""
        return str(path) in self._done

    def open(self) -> None:
        self._handle = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - closed in close()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> RunJournal:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record(self, path: Path, system: str, method: str, *, matched: bool) -> None:
        if self._handle is None:
            return
        self._done.add(str(path))
        self._handle.write(
            json.dumps(
                {"path": str(path), "system": system, "method": method, "matched": matched, "at": time.time()},
                ensure_ascii=False,
            )
            + "\n"
        )
        # Flush per line: an interrupted run must not lose the last few minutes
        # of work to a buffer that never reached disk.
        self._handle.flush()

    def clear(self) -> None:
        self.close()
        self.path.unlink(missing_ok=True)
        self._done = set()
