"""Quota accounting and concurrency governance.

ScreenScraper reports the caller's live allowance in ``response.ssuser`` on
*every* response, and those numbers move during a run: threads granted depends
on current server load, not just account level.  So we re-read them from every
response and adapt, rather than reading them once at startup and hoping.

Two budgets matter, and conflating them is the classic scraper bug:

* the **main request quota** (``maxrequestsperday``), spent by every lookup, and
* the **KO quota** (``maxrequestskoperday``), spent only by lookups that match
  nothing.  It is far smaller.  A library with homebrew, hacks or oddly-named
  ROMs exhausts it long before the main quota, and the resulting failure looks
  baffling unless it is tracked separately from request one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import QuotaExceededError

__all__ = ["QuotaGovernor", "QuotaSnapshot"]


def _as_int(value: Any, default: int = 0) -> int:
    """Parse a quota field.  ScreenScraper sends these as strings, sometimes empty."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
        return int(float(text)) if text else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class QuotaSnapshot:
    """The caller's allowance as of the most recent response."""

    max_threads: int = 1
    max_requests_per_min: int = 0
    max_requests_per_day: int = 0
    requests_today: int = 0
    max_requests_ko_per_day: int = 0
    requests_ko_today: int = 0
    max_download_speed: int = 0
    level: int = 0
    username: str = ""

    @classmethod
    def from_ssuser(cls, payload: Mapping[str, Any]) -> QuotaSnapshot:
        return cls(
            max_threads=max(1, _as_int(payload.get("maxthreads"), 1)),
            max_requests_per_min=_as_int(payload.get("maxrequestspermin")),
            max_requests_per_day=_as_int(payload.get("maxrequestsperday")),
            requests_today=_as_int(payload.get("requeststoday")),
            max_requests_ko_per_day=_as_int(payload.get("maxrequestskoperday")),
            requests_ko_today=_as_int(payload.get("requestskotoday")),
            max_download_speed=_as_int(payload.get("maxdownloadspeed")),
            level=_as_int(payload.get("niveau")),
            username=str(payload.get("id") or ""),
        )

    @property
    def requests_remaining(self) -> int | None:
        if self.max_requests_per_day <= 0:
            return None
        return max(0, self.max_requests_per_day - self.requests_today)

    @property
    def requests_pct(self) -> float | None:
        if self.max_requests_per_day <= 0:
            return None
        return 100.0 * self.requests_today / self.max_requests_per_day


class _ResizableSemaphore:
    """A semaphore whose limit can change while tasks are waiting on it.

    ``asyncio.Semaphore`` fixes its value at construction, but the thread
    allowance ScreenScraper grants us can change mid-run, so the limit has to be
    adjustable without tearing down in-flight work.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    async def set_limit(self, limit: int) -> None:
        limit = max(1, limit)
        async with self._condition:
            if limit == self._limit:
                return
            self._limit = limit
            # Wake everyone: a raised limit may admit several waiters at once,
            # and a lowered one simply finds them blocked again.
            self._condition.notify_all()

    async def acquire(self) -> None:
        async with self._condition:
            while self._active >= self._limit:
                await self._condition.wait()
            self._active += 1

    async def release(self) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify()


class QuotaGovernor:
    """Gate every API request on the live allowance.

    Responsibilities, in the order they bite:

    1. Cap concurrency at the server-granted ``maxthreads`` (``--jobs`` may only
       lower this, never raise it).
    2. Space requests to at most ``maxrequestspermin``.
    3. Stop the run at ``stop_at_quota_pct`` of either budget, leaving headroom
       rather than discovering the ceiling by slamming into it.
    """

    def __init__(
        self,
        *,
        jobs_cap: int | None = None,
        stop_at_quota_pct: float = 95.0,
        initial: QuotaSnapshot | None = None,
    ) -> None:
        self._jobs_cap = jobs_cap if jobs_cap is None or jobs_cap > 0 else None
        self._stop_at_pct = stop_at_quota_pct
        self._snapshot = initial or QuotaSnapshot()
        self._semaphore = _ResizableSemaphore(self._effective_threads(self._snapshot))
        self._spacing_lock = asyncio.Lock()
        self._next_allowed = 0.0
        self._interval = self._spacing_for(self._snapshot)
        self._ko_observed = 0

    @property
    def snapshot(self) -> QuotaSnapshot:
        return self._snapshot

    @property
    def concurrency(self) -> int:
        return self._semaphore.limit

    def _effective_threads(self, snapshot: QuotaSnapshot) -> int:
        threads = max(1, snapshot.max_threads)
        if self._jobs_cap is not None:
            threads = min(threads, self._jobs_cap)
        return threads

    @staticmethod
    def _spacing_for(snapshot: QuotaSnapshot) -> float:
        if snapshot.max_requests_per_min <= 0:
            return 0.0
        return 60.0 / snapshot.max_requests_per_min

    async def update(self, ssuser: Mapping[str, Any] | None) -> None:
        """Absorb the ``ssuser`` block from a response and re-tune."""
        if not ssuser:
            return
        await self.apply(QuotaSnapshot.from_ssuser(ssuser))

    async def apply(self, snapshot: QuotaSnapshot) -> None:
        self._snapshot = snapshot
        self._interval = self._spacing_for(snapshot)
        await self._semaphore.set_limit(self._effective_threads(snapshot))

    def note_ko(self) -> None:
        """Record a lookup that matched nothing.

        The server's own ``requestskotoday`` only refreshes on the next
        response, so we track KO locally too and take whichever is higher --
        otherwise a burst of concurrent misses can overshoot the KO budget
        before the server's count catches up.
        """
        self._ko_observed += 1

    @property
    def ko_used(self) -> int:
        return max(self._snapshot.requests_ko_today, self._ko_observed)

    def check_budget(self) -> None:
        """Raise :class:`QuotaExceededError` if either budget is near its limit."""
        snapshot = self._snapshot

        pct = snapshot.requests_pct
        if pct is not None and pct >= self._stop_at_pct:
            raise QuotaExceededError(
                f"Daily request quota at {pct:.1f}% "
                f"({snapshot.requests_today}/{snapshot.max_requests_per_day}); "
                f"stopping at the configured {self._stop_at_pct:.0f}% threshold."
            )

        if snapshot.max_requests_ko_per_day > 0:
            ko_pct = 100.0 * self.ko_used / snapshot.max_requests_ko_per_day
            if ko_pct >= self._stop_at_pct:
                raise QuotaExceededError(
                    f"Not-found (KO) quota at {ko_pct:.1f}% "
                    f"({self.ko_used}/{snapshot.max_requests_ko_per_day}). "
                    "This is the smaller budget, spent by ROMs that match nothing."
                )

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Reserve one request's worth of concurrency and rate allowance."""
        self.check_budget()
        await self._semaphore.acquire()
        try:
            await self._space()
            yield
        finally:
            await self._semaphore.release()

    async def _space(self) -> None:
        if self._interval <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._spacing_lock:
            now = loop.time()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_allowed = now + self._interval
