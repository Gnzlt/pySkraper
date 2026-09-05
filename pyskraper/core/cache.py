"""Local SQLite cache.

Everything is keyed on **content hash rather than path**, so reorganising or
renaming a library costs nothing and a ROM that moves between systems is still
recognised.

The ``misses`` table is the one that earns its keep fastest.  A lookup that
matches nothing spends the *KO quota* -- a separate budget, far smaller than
the main one -- so re-asking about the same unmatchable homebrew ROM on every
run is how a library with a hundred oddities burns its daily allowance before
touching the games that would have matched.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hasher import Hashes

__all__ = ["Cache", "CacheStats"]

# Databases written by earlier versions also carry an empty `media` table.
# Nothing ever populated it and nothing reads it now, so it is left where it
# is -- but no statement here may name it, or every freshly created cache
# would fail on a table it never had.
SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (
    path   TEXT PRIMARY KEY,
    size   INTEGER NOT NULL,
    mtime  REAL    NOT NULL,
    crc32  TEXT    NOT NULL,
    md5    TEXT    NOT NULL,
    sha1   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    md5         TEXT NOT NULL,
    systemeid   INTEGER NOT NULL,
    crc32       TEXT,
    sha1        TEXT,
    ss_game_id  INTEGER,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (md5, systemeid)
);

CREATE TABLE IF NOT EXISTS misses (
    md5          TEXT NOT NULL,
    systemeid    INTEGER NOT NULL,
    romnom       TEXT,
    reason       TEXT,
    attempted_at REAL NOT NULL,
    PRIMARY KEY (md5, systemeid)
);
"""

DAY = 86400.0
GAME_TTL = 30 * DAY
MISS_TTL = 7 * DAY


@dataclass(frozen=True)
class CacheStats:
    hashes: int
    games: int
    misses: int
    size_bytes: int


class Cache:
    """A small, synchronous SQLite wrapper guarded by a lock.

    Hashing runs in a thread pool while lookups happen on the event loop, so
    the connection is shared across threads and every statement is serialised.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            # Every `hashes` lookup goes through the `path` primary key, so
            # this index was maintained on each insert and read by nothing.
            self._db.execute("DROP INDEX IF EXISTS hashes_md5")
            # WAL keeps a reader from blocking the writer, which matters when
            # hashing threads and the event loop touch the cache concurrently.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- hashes ----------------------------------------------------------

    def get_hashes(self, path: Path, size: int, mtime: float) -> Hashes | None:
        """Return cached hashes, but only if the file is byte-for-byte the same.

        Keyed on ``(path, size, mtime)``: if any of the three changed, the
        cached hashes describe a different file and must be recomputed.  This is
        what makes uncapped hashing of a 3,000-ROM library practical.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT size, mtime, crc32, md5, sha1 FROM hashes WHERE path = ?", (str(path),)
            ).fetchone()

        if row is None or row["size"] != size or abs(row["mtime"] - mtime) > 1e-6:
            return None
        return Hashes(crc32=row["crc32"], md5=row["md5"], sha1=row["sha1"], size=size)

    def hash_record(self, path: Path) -> Hashes | None:
        """The stored hashes for this path, whatever the file looks like now.

        ``get_hashes`` deliberately refuses a record whose size or mtime moved,
        because for identification a stale hash is worse than none.  ``verify``
        wants precisely that stale record: it is the "before" half of the
        comparison that detects a ROM whose content changed underneath us.
        """
        with self._lock:
            row = self._db.execute("SELECT size, crc32, md5, sha1 FROM hashes WHERE path = ?", (str(path),)).fetchone()
        if row is None:
            return None
        return Hashes(crc32=row["crc32"], md5=row["md5"], sha1=row["sha1"], size=int(row["size"]))

    def put_hashes(self, path: Path, size: int, mtime: float, hashes: Hashes) -> None:
        if hashes.truncated:
            # A prefix hash identifies nothing; caching it would make the
            # useless value sticky.
            return
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO hashes (path, size, mtime, crc32, md5, sha1) VALUES (?, ?, ?, ?, ?, ?)",
                (str(path), size, mtime, hashes.crc32, hashes.md5, hashes.sha1),
            )
            self._db.commit()

    # ---- games -----------------------------------------------------------

    def get_game(self, md5: str, systeme_id: int, *, ttl: float = GAME_TTL) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload, fetched_at FROM games WHERE md5 = ? AND systemeid = ?", (md5, systeme_id)
            ).fetchone()
        if row is None or time.time() - row["fetched_at"] > ttl:
            return None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def put_game(self, md5: str, systeme_id: int, game: dict[str, Any], *, hashes: Hashes | None = None) -> None:
        game_id: int | None = None
        raw = game.get("id")
        if raw is not None:
            try:
                game_id = int(str(raw))
            except ValueError:
                game_id = None

        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO games (md5, systemeid, crc32, sha1, ss_game_id, payload, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    md5,
                    systeme_id,
                    hashes.crc32 if hashes else None,
                    hashes.sha1 if hashes else None,
                    game_id,
                    json.dumps(game),
                    time.time(),
                ),
            )
            self._db.commit()

    # ---- misses ----------------------------------------------------------

    def is_miss(self, md5: str, systeme_id: int, *, ttl: float = MISS_TTL) -> bool:
        """True if this ROM already failed to match recently.

        Short-circuiting here is what protects the KO quota across runs.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT attempted_at FROM misses WHERE md5 = ? AND systemeid = ?", (md5, systeme_id)
            ).fetchone()
        return row is not None and time.time() - row["attempted_at"] <= ttl

    def put_miss(self, md5: str, systeme_id: int, *, rom_name: str = "", reason: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO misses (md5, systemeid, romnom, reason, attempted_at) VALUES (?, ?, ?, ?, ?)",
                (md5, systeme_id, rom_name, reason, time.time()),
            )
            self._db.commit()

    def forget_miss(self, md5: str, systeme_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM misses WHERE md5 = ? AND systemeid = ?", (md5, systeme_id))
            self._db.commit()

    # ---- maintenance -----------------------------------------------------

    def stats(self) -> CacheStats:
        with self._lock:
            counts = {
                table: int(self._db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
                for table in ("hashes", "games", "misses")
            }
        size = self.path.stat().st_size if self.path.exists() else 0
        return CacheStats(
            hashes=counts["hashes"],
            games=counts["games"],
            misses=counts["misses"],
            size_bytes=size,
        )

    def clear(self, *, systeme_id: int | None = None, tables: tuple[str, ...] | None = None) -> None:
        targets = tables or ("games", "misses")
        with self._lock:
            for table in targets:
                if systeme_id is not None and table in ("games", "misses"):
                    self._db.execute(f"DELETE FROM {table} WHERE systemeid = ?", (systeme_id,))
                elif systeme_id is None:
                    self._db.execute(f"DELETE FROM {table}")
            self._db.commit()

    def prune(self, *, game_ttl: float = GAME_TTL, miss_ttl: float = MISS_TTL) -> int:
        now = time.time()
        with self._lock:
            removed = self._db.execute("DELETE FROM games WHERE ? - fetched_at > ?", (now, game_ttl)).rowcount
            removed += self._db.execute("DELETE FROM misses WHERE ? - attempted_at > ?", (now, miss_ttl)).rowcount
            self._db.commit()
        return int(removed)
