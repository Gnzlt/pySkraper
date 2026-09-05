"""Async HTTP client for the ScreenScraper v2 API."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .. import __version__
from ..logging_setup import register_secret
from .errors import RateLimitError, ScreenScraperError, TransientError, classify
from .quota import QuotaGovernor, QuotaSnapshot

__all__ = ["ApiCredentials", "ScreenScraperClient"]

log = logging.getLogger(__name__)

BASE_URL = "https://api.screenscraper.fr/api2"


@dataclass(frozen=True)
class ApiCredentials:
    """The four secrets plus the software identifier every call carries."""

    devid: str
    devpassword: str
    ssid: str
    sspassword: str
    softname: str = "pySkraper"

    def register(self) -> None:
        """Teach the log formatter never to print these."""
        register_secret(self.devpassword)
        register_secret(self.sspassword)
        register_secret(self.devid)
        register_secret(self.ssid)

    def as_params(self) -> dict[str, str]:
        return {
            "devid": self.devid,
            "devpassword": self.devpassword,
            "ssid": self.ssid,
            "sspassword": self.sspassword,
            "softname": self.softname,
            "output": "json",
        }


class ScreenScraperClient:
    """One pooled ``httpx.AsyncClient`` plus the retry and quota discipline.

    Auth parameters are injected centrally in :meth:`_request` so no call site
    can forget them -- or log them.
    """

    def __init__(
        self,
        credentials: ApiCredentials,
        governor: QuotaGovernor,
        *,
        timeout: float = 30.0,
        retries: int = 4,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        credentials.register()
        self._credentials = credentials
        self._governor = governor
        self._retries = max(0, retries)
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=True,
            # ScreenScraper is the one party who might have to identify a
            # misbehaving client version, so this tracks __version__ rather
            # than a literal that stopped being true after the first release.
            headers={"User-Agent": f"{credentials.softname}/{__version__}"},
        )

    async def __aenter__(self) -> ScreenScraperClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def governor(self) -> QuotaGovernor:
        return self._governor

    @property
    def http(self) -> httpx.AsyncClient:
        return self._client

    async def _request(self, endpoint: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Issue one API call, with retries, and feed the governor the result."""
        merged: dict[str, str] = self._credentials.as_params()
        for key, value in params.items():
            if value is not None and value != "":
                merged[key] = str(value)

        url = f"{self._base_url}/{endpoint}"
        last_error: ScreenScraperError | None = None

        for attempt in range(self._retries + 1):
            async with self._governor.slot():
                try:
                    response = await self._client.get(url, params=merged)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = TransientError(f"{type(exc).__name__}: {exc}")
                else:
                    payload_or_error = await self._interpret(response)
                    if isinstance(payload_or_error, dict):
                        return payload_or_error
                    last_error = payload_or_error

            if not last_error.retryable:
                raise last_error
            if attempt < self._retries:
                await asyncio.sleep(self._backoff(attempt))
                log.debug("retrying %s after %s (attempt %d)", endpoint, type(last_error).__name__, attempt + 1)

        # Not an assert: `python -O` strips those, and this one degrades to
        # `raise None` -- turning the API error four attempts collected into a
        # bare TypeError from the raise statement itself.
        if last_error is None:  # pragma: no cover - the loop always sets it
            raise RuntimeError(f"{endpoint}: retries exhausted with no error recorded")
        raise last_error

    async def _interpret(self, response: httpx.Response) -> dict[str, Any] | ScreenScraperError:
        """Classify, then parse.  Never parse first."""
        body = response.text

        error = classify(response.status_code, body)
        if error is not None:
            # Even a failure carries a fresh ssuser block often enough to be
            # worth harvesting -- but only if the body is actually JSON.
            await self._absorb_quota(body)
            return error

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            # ScreenScraper signals its per-minute thread ceiling by truncating
            # the response rather than by returning a status code, so an
            # unparseable body is a rate-limit signal, not a parser bug.
            return RateLimitError("Unparseable response body (truncated - usually the per-minute thread limit)")

        if not isinstance(payload, dict):
            return TransientError(f"Unexpected top-level JSON type: {type(payload).__name__}")

        inner = payload.get("response")
        if not isinstance(inner, dict):
            return TransientError("Response JSON has no 'response' object")

        await self._governor.update(inner.get("ssuser"))
        return inner

    async def _absorb_quota(self, body: str) -> None:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            inner = payload.get("response")
            if isinstance(inner, dict):
                await self._governor.update(inner.get("ssuser"))

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with jitter, capped so a run cannot stall for minutes."""
        return min(30.0, (2.0**attempt)) * (0.5 + random.random() / 2)

    # ---- endpoints -------------------------------------------------------

    @property
    def anonymous(self) -> bool:
        """True when running on developer credentials alone."""
        return not (self._credentials.ssid.strip() and self._credentials.sspassword.strip())

    async def user_info(self) -> QuotaSnapshot:
        """Prime the governor before the run, so concurrency is right from request one.

        ``ssuserInfos.php`` requires a member account and returns 403 without
        one. ``systemesListe.php`` carries the same ``ssuser`` block and works on
        developer credentials alone, so it is the anonymous-mode equivalent.
        """
        endpoint = "systemesListe.php" if self.anonymous else "ssuserInfos.php"
        payload = await self._request(endpoint, {})
        ssuser = payload.get("ssuser")
        snapshot = QuotaSnapshot.from_ssuser(ssuser if isinstance(ssuser, Mapping) else {})
        await self._governor.apply(snapshot)
        return snapshot

    async def systems_list(self) -> list[dict[str, Any]]:
        payload = await self._request("systemesListe.php", {})
        systems = payload.get("systemes")
        return [s for s in systems if isinstance(s, dict)] if isinstance(systems, list) else []

    async def game_info(
        self,
        *,
        systeme_id: int | None = None,
        rom_name: str | None = None,
        rom_size: int | None = None,
        crc: str | None = None,
        md5: str | None = None,
        sha1: str | None = None,
        serial: str | None = None,
        game_id: int | None = None,
        rom_type: str = "rom",
    ) -> dict[str, Any]:
        """Look one game up.  Hashes lead; everything else is a fallback."""
        params: dict[str, Any] = {
            "systemeid": systeme_id,
            "romtype": rom_type,
            "romnom": rom_name,
            "romtaille": rom_size,
            "crc": crc,
            "md5": md5,
            "sha1": sha1,
            "serialnum": serial,
            "gameid": game_id,
        }
        payload = await self._request("jeuInfos.php", params)
        game = payload.get("jeu")
        if not isinstance(game, dict):
            return {}
        return game

    async def search(self, *, systeme_id: int | None, term: str) -> list[dict[str, Any]]:
        payload = await self._request("jeuRecherche.php", {"systemeid": systeme_id, "recherche": term})
        games = payload.get("jeux")
        return [g for g in games if isinstance(g, dict)] if isinstance(games, list) else []
