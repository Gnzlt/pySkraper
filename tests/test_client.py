"""The HTTP client: auth injection, error classification in context, retry
behaviour, and quota harvesting. No test here touches the network."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from pyskraper.api.client import ApiCredentials, ScreenScraperClient
from pyskraper.api.errors import AuthError, NotFoundError, QuotaExceededError, RateLimitError
from pyskraper.api.quota import QuotaGovernor

BASE = "https://api.screenscraper.fr/api2"

CREDS = ApiCredentials(devid="dev", devpassword="devpw", ssid="user", sspassword="userpw", softname="pySkraper")


def _ssuser(**overrides: Any) -> dict[str, Any]:
    payload = {
        "id": "user",
        "niveau": "2",
        "maxthreads": "3",
        "maxrequestspermin": "0",
        "maxrequestsperday": "10000",
        "requeststoday": "42",
        "maxrequestskoperday": "200",
        "requestskotoday": "1",
        "maxdownloadspeed": "500",
    }
    payload.update(overrides)
    return payload


def _ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, text=json.dumps({"header": {}, "response": body}))


def _client(retries: int = 0) -> ScreenScraperClient:
    return ScreenScraperClient(CREDS, QuotaGovernor(), retries=retries)


@respx.mock
async def test_user_info_primes_the_governor() -> None:
    respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": _ssuser()}))
    async with _client() as client:
        snapshot = await client.user_info()
    assert snapshot.max_threads == 3
    assert snapshot.requests_today == 42
    assert client.governor.concurrency == 3


@respx.mock
async def test_every_request_carries_auth_and_json_output() -> None:
    route = respx.get(f"{BASE}/ssuserInfos.php").mock(return_value=_ok({"ssuser": _ssuser()}))
    async with _client() as client:
        await client.user_info()

    params = route.calls[0].request.url.params
    assert params["devid"] == "dev"
    assert params["devpassword"] == "devpw"
    assert params["ssid"] == "user"
    assert params["sspassword"] == "userpw"
    assert params["softname"] == "pySkraper"
    assert params["output"] == "json"


@respx.mock
async def test_game_info_sends_all_three_hashes() -> None:
    """Hash-first identification: three hashes on one request, not three requests."""
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": {"id": "1"}, "ssuser": _ssuser()}))
    async with _client() as client:
        await client.game_info(
            systeme_id=4, rom_name="Game.sfc", rom_size=524288, crc="B19ED489", md5="abc", sha1="def"
        )

    params = route.calls[0].request.url.params
    assert params["crc"] == "B19ED489"
    assert params["md5"] == "abc"
    assert params["sha1"] == "def"
    assert params["systemeid"] == "4"
    assert params["romtaille"] == "524288"
    assert params["romtype"] == "rom"


@respx.mock
async def test_empty_parameters_are_omitted_entirely() -> None:
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": {}, "ssuser": _ssuser()}))
    async with _client() as client:
        await client.game_info(systeme_id=4, rom_name="Game.sfc", serial=None, game_id=None)

    params = route.calls[0].request.url.params
    assert "serialnum" not in params
    assert "gameid" not in params


@respx.mock
async def test_quota_is_harvested_from_every_response() -> None:
    """The thread grant moves with server load, so it is re-read continuously
    rather than trusted from startup."""
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"jeu": {}, "ssuser": _ssuser(maxthreads="9")}))
    async with _client() as client:
        assert client.governor.concurrency == 1
        await client.game_info(systeme_id=4, rom_name="x.sfc")
        assert client.governor.concurrency == 9


@respx.mock
async def test_auth_error_is_fatal_and_not_retried() -> None:
    route = respx.get(f"{BASE}/ssuserInfos.php").mock(
        return_value=httpx.Response(401, text="Erreur de login : vérifiez vos identifiants")
    )
    async with _client(retries=3) as client:
        with pytest.raises(AuthError):
            await client.user_info()
    assert route.call_count == 1, "a bad password cannot be fixed by asking again"


@respx.mock
async def test_not_found_surfaces_as_notfound() -> None:
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=httpx.Response(404, text="Rom non trouvée !"))
    async with _client() as client:
        with pytest.raises(NotFoundError):
            await client.game_info(systeme_id=4, rom_name="Nonexistent.sfc")


@respx.mock
async def test_quota_exceeded_is_fatal() -> None:
    respx.get(f"{BASE}/jeuInfos.php").mock(
        return_value=httpx.Response(430, text="Votre quota de scrape est dépassé pour aujourd'hui")
    )
    async with _client(retries=2) as client:
        with pytest.raises(QuotaExceededError):
            await client.game_info(systeme_id=4, rom_name="x.sfc")


@respx.mock
async def test_truncated_body_is_treated_as_a_rate_limit() -> None:
    """ScreenScraper signals its per-minute thread ceiling by truncating the
    response rather than returning a status code. Parsing that as a bug instead
    of a rate limit is how scrapers end up hammering a server that said stop.
    """
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=httpx.Response(200, text='{"response": {"jeu": {"id'))
    async with _client(retries=0) as client:
        with pytest.raises(RateLimitError):
            await client.game_info(systeme_id=4, rom_name="x.sfc")


@respx.mock
async def test_transient_failure_is_retried_then_succeeds() -> None:
    route = respx.get(f"{BASE}/jeuInfos.php").mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            _ok({"jeu": {"id": "7"}, "ssuser": _ssuser()}),
        ]
    )
    async with _client(retries=2) as client:
        game = await client.game_info(systeme_id=4, rom_name="x.sfc")
    assert game["id"] == "7"
    assert route.call_count == 2


@respx.mock
async def test_retries_are_bounded() -> None:
    route = respx.get(f"{BASE}/jeuInfos.php").mock(return_value=httpx.Response(503, text="nope"))
    async with _client(retries=2) as client:
        with pytest.raises(Exception, match="nope"):
            await client.game_info(systeme_id=4, rom_name="x.sfc")
    assert route.call_count == 3, "initial attempt plus two retries"


@respx.mock
async def test_network_error_is_retried() -> None:
    route = respx.get(f"{BASE}/jeuInfos.php").mock(
        side_effect=[httpx.ConnectTimeout("timed out"), _ok({"jeu": {"id": "1"}, "ssuser": _ssuser()})]
    )
    async with _client(retries=1) as client:
        await client.game_info(systeme_id=4, rom_name="x.sfc")
    assert route.call_count == 2


@respx.mock
async def test_missing_game_object_returns_empty_not_an_exception() -> None:
    respx.get(f"{BASE}/jeuInfos.php").mock(return_value=_ok({"ssuser": _ssuser()}))
    async with _client() as client:
        assert await client.game_info(systeme_id=4, rom_name="x.sfc") == {}
