"""Quota governance. The KO budget assertions here are the important ones --
it is the smaller of the two budgets and the one that actually ends runs."""

from __future__ import annotations

import asyncio

import pytest

from pyskraper.api.errors import QuotaExceededError
from pyskraper.api.quota import QuotaGovernor, QuotaSnapshot


def _ssuser(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "someone",
        "niveau": "3",
        "maxthreads": "4",
        "maxrequestspermin": "300",
        "maxrequestsperday": "20000",
        "requeststoday": "100",
        "maxrequestskoperday": "500",
        "requestskotoday": "5",
        "maxdownloadspeed": "1000",
    }
    payload.update(overrides)
    return payload


def test_parses_string_fields_the_api_actually_sends() -> None:
    snapshot = QuotaSnapshot.from_ssuser(_ssuser())
    assert snapshot.max_threads == 4
    assert snapshot.max_requests_per_day == 20000
    assert snapshot.requests_today == 100
    assert snapshot.max_requests_ko_per_day == 500


def test_tolerates_missing_and_empty_fields() -> None:
    snapshot = QuotaSnapshot.from_ssuser({"maxthreads": "", "requeststoday": None})
    assert snapshot.max_threads == 1
    assert snapshot.requests_today == 0
    assert snapshot.requests_remaining is None


async def test_concurrency_follows_the_server_grant() -> None:
    governor = QuotaGovernor()
    assert governor.concurrency == 1
    await governor.update(_ssuser(maxthreads="8"))
    assert governor.concurrency == 8


async def test_jobs_flag_can_only_lower_concurrency() -> None:
    governor = QuotaGovernor(jobs_cap=2)
    await governor.update(_ssuser(maxthreads="16"))
    assert governor.concurrency == 2, "--jobs must never raise us above maxthreads"


async def test_semaphore_resizes_while_tasks_are_waiting() -> None:
    governor = QuotaGovernor(initial=QuotaSnapshot(max_threads=1))
    running = asyncio.Event()
    release = asyncio.Event()
    admitted = 0

    async def occupy() -> None:
        nonlocal admitted
        async with governor.slot():
            admitted += 1
            running.set()
            await release.wait()

    first = asyncio.create_task(occupy())
    await running.wait()

    second = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    assert admitted == 1, "second task must be blocked at a limit of 1"

    await governor.update(_ssuser(maxthreads="4", maxrequestspermin="0"))
    await asyncio.sleep(0.01)
    assert admitted == 2, "raising the limit must wake a waiter"

    release.set()
    await asyncio.gather(first, second)


async def test_requests_are_spaced_by_the_per_minute_allowance() -> None:
    governor = QuotaGovernor(initial=QuotaSnapshot(max_threads=4, max_requests_per_min=600))
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(3):
        async with governor.slot():
            pass
    # 600/min -> 0.1s apart; three slots means at least two gaps.
    assert loop.time() - start >= 0.15


async def test_stops_before_the_main_quota_is_actually_exhausted() -> None:
    governor = QuotaGovernor(stop_at_quota_pct=95.0)
    await governor.update(_ssuser(maxrequestsperday="1000", requeststoday="950"))
    with pytest.raises(QuotaExceededError, match="Daily request quota"):
        governor.check_budget()


async def test_ko_quota_stops_the_run_independently() -> None:
    """The KO budget is far smaller than the main one and is spent only by
    ROMs that match nothing, so it must be able to halt a run on its own."""
    governor = QuotaGovernor(stop_at_quota_pct=95.0)
    await governor.update(_ssuser(maxrequestsperday="20000", requeststoday="10", maxrequestskoperday="100"))
    governor.check_budget()  # plenty of main quota left

    for _ in range(95):
        governor.note_ko()

    with pytest.raises(QuotaExceededError, match="KO"):
        governor.check_budget()


async def test_local_ko_count_leads_the_server_count() -> None:
    """Concurrent misses can overshoot the KO budget before the server's
    counter catches up, so we track locally and take the higher value."""
    governor = QuotaGovernor()
    await governor.update(_ssuser(requestskotoday="5"))
    assert governor.ko_used == 5
    for _ in range(10):
        governor.note_ko()
    assert governor.ko_used == 10
