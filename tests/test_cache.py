"""The cache. Keyed on content, not paths -- and the misses table is what
protects the KO quota across runs."""

from __future__ import annotations

import time
from pathlib import Path

from pyskraper.core.cache import Cache
from pyskraper.core.hasher import Hashes

H = Hashes(crc32="B19ED489", md5="abc123", sha1="def456", size=1024)


def _cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache.db")


def test_hashes_round_trip(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    rom = tmp_path / "game.sfc"
    cache.put_hashes(rom, 1024, 1000.0, H)
    assert cache.get_hashes(rom, 1024, 1000.0) == H


def test_changed_mtime_invalidates(tmp_path: Path) -> None:
    """(path, size, mtime) is the key: if any changed, the cached hashes
    describe a different file."""
    cache = _cache(tmp_path)
    rom = tmp_path / "game.sfc"
    cache.put_hashes(rom, 1024, 1000.0, H)
    assert cache.get_hashes(rom, 1024, 2000.0) is None


def test_changed_size_invalidates(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    rom = tmp_path / "game.sfc"
    cache.put_hashes(rom, 1024, 1000.0, H)
    assert cache.get_hashes(rom, 2048, 1000.0) is None


def test_truncated_hashes_are_never_cached(tmp_path: Path) -> None:
    """A prefix hash identifies nothing; caching it would make a useless value
    sticky."""
    cache = _cache(tmp_path)
    rom = tmp_path / "big.chd"
    cache.put_hashes(rom, 999, 1.0, Hashes(crc32="X", md5="y", sha1="z", size=999, truncated=True))
    assert cache.get_hashes(rom, 999, 1.0) is None


def test_game_round_trip(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put_game("abc123", 4, {"id": "1234", "noms": []}, hashes=H)
    assert cache.get_game("abc123", 4) == {"id": "1234", "noms": []}


def test_game_cache_is_per_system(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put_game("abc123", 4, {"id": "1"}, hashes=H)
    assert cache.get_game("abc123", 57) is None


def test_expired_game_is_ignored(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put_game("abc123", 4, {"id": "1"}, hashes=H)
    assert cache.get_game("abc123", 4, ttl=-1) is None


def test_miss_is_remembered_then_expires(tmp_path: Path) -> None:
    """This is what stops a library of homebrew from re-burning the KO budget
    every single run."""
    cache = _cache(tmp_path)
    cache.put_miss("abc123", 4, rom_name="weird.sfc")
    assert cache.is_miss("abc123", 4)
    assert not cache.is_miss("abc123", 4, ttl=-1)


def test_a_later_match_clears_the_miss(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put_miss("abc123", 4)
    cache.forget_miss("abc123", 4)
    assert not cache.is_miss("abc123", 4)


def test_stats_and_clear(tmp_path: Path) -> None:
    """Also the guard on table names: this runs against a freshly created
    database, so any statement naming a table the schema no longer creates
    fails here rather than on a user's machine."""
    cache = _cache(tmp_path)
    cache.put_hashes(tmp_path / "a.sfc", 1, 1.0, H)
    cache.put_game("abc123", 4, {"id": "1"}, hashes=H)
    cache.put_miss("zzz", 4)

    stats = cache.stats()
    assert stats.hashes == 1 and stats.games == 1 and stats.misses == 1

    cache.clear()
    after = cache.stats()
    assert after.games == 0 and after.misses == 0
    assert after.hashes == 1, "clearing API data should not force a full re-hash"


def test_clear_can_target_one_system(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put_game("a", 4, {"id": "1"}, hashes=H)
    cache.put_game("b", 57, {"id": "2"}, hashes=H)
    cache.clear(systeme_id=4)
    assert cache.get_game("a", 4) is None
    assert cache.get_game("b", 57) is not None


def test_prune_removes_only_stale_rows(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put_game("fresh", 4, {"id": "1"}, hashes=H)
    removed = cache.prune(game_ttl=3600, miss_ttl=3600)
    assert removed == 0
    assert cache.get_game("fresh", 4) is not None

    assert cache.prune(game_ttl=-1, miss_ttl=-1) >= 1


def test_reopening_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "cache.db"
    with Cache(path) as cache:
        cache.put_game("abc", 4, {"id": "9"}, hashes=H)
    with Cache(path) as reopened:
        assert reopened.get_game("abc", 4) == {"id": "9"}


def test_corrupt_payload_is_survivable(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put_game("abc", 4, {"id": "1"}, hashes=H)
    # Deliberately reaching into the connection to simulate on-disk corruption.
    with cache._lock:
        cache._db.execute("UPDATE games SET payload = 'not json' WHERE md5 = 'abc'")
        cache._db.commit()
    assert cache.get_game("abc", 4) is None


def test_time_is_recorded(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    before = time.time()
    cache.put_miss("x", 4)
    assert cache.is_miss("x", 4, ttl=time.time() - before + 10)
