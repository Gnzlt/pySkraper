"""Failure classification for the ScreenScraper API.

ScreenScraper signals failure inconsistently: sometimes an HTTP status code,
sometimes a French sentence inside a ``200`` body, sometimes an HTML page, and
sometimes a body that is simply truncated mid-stream.  Every response therefore
passes through :func:`classify` *before* it is parsed, and every failure mode
gets a named class carrying explicit retry and abort semantics.
"""

from __future__ import annotations

import html
import re
import unicodedata

__all__ = [
    "ApiClosedError",
    "AuthError",
    "BlacklistedError",
    "NotFoundError",
    "QuotaExceededError",
    "RateLimitError",
    "ScreenScraperError",
    "TransientError",
    "classify",
    "looks_like_json",
    "strip_html",
]


class ScreenScraperError(Exception):
    """Base class for every ScreenScraper failure.

    Attributes:
        retryable: the same request may succeed if repeated after a backoff.
        fatal: the run cannot continue; keep going would be pointless or rude.
    """

    retryable: bool = False
    fatal: bool = False

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


class AuthError(ScreenScraperError):
    """Bad developer or user credentials.  Retrying cannot help."""

    fatal = True


class BlacklistedError(ScreenScraperError):
    """This software or user has been blacklisted by ScreenScraper."""

    fatal = True


class NotFoundError(ScreenScraperError):
    """The game was not found.

    Not fatal: the run continues.  But it consumes the *KO quota*, which is a
    separate and much smaller budget than the main request quota, so callers
    must record it rather than treat it as a silent no-op.
    """


class ApiClosedError(ScreenScraperError):
    """The API is closed — either entirely, or to non-members under load."""

    fatal = True


class RateLimitError(ScreenScraperError):
    """Too many requests, or too many threads.  Back off and retry.

    Also raised for a body we cannot parse at all: ScreenScraper signals its
    per-minute thread limit by truncating the response rather than by returning
    a status code, so an unparseable body is a rate-limit signal, not a bug.
    """

    retryable = True


class QuotaExceededError(ScreenScraperError):
    """The daily scrape quota is spent.  Stop cleanly and keep the journal."""

    fatal = True


class TransientError(ScreenScraperError):
    """A server-side or network hiccup, or a response we do not recognise."""

    retryable = True


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(body: str) -> str:
    """Reduce an HTML error page to its text.

    Several conditions are reported as an HTML page with a ``200`` status, so
    the message we surface to the user has to come out of the markup.
    """
    text = _TAG_RE.sub(" ", body)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def _fold(text: str) -> str:
    """Lowercase and strip accents, so French markers match however they arrive."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", without_accents.lower()).strip()


# Ordered: the first marker found in the body wins.  More specific phrases must
# come before more general ones.
_MARKERS: tuple[tuple[str, type[ScreenScraperError]], ...] = (
    ("erreur de login", AuthError),
    ("bad password", AuthError),
    ("mot de passe incorrect", AuthError),
    ("blackliste", BlacklistedError),
    ("api totalement ferme", ApiClosedError),
    ("api fermee", ApiClosedError),
    ("non membres", ApiClosedError),
    ("closed for non-members", ApiClosedError),
    ("quota de scrape est depasse", QuotaExceededError),
    ("quota journalier", QuotaExceededError),
    ("maximum threads per minute reached", RateLimitError),
    ("nombre de threads", RateLimitError),
    ("non trouve", NotFoundError),
)

_STATUS_MAP: dict[int, type[ScreenScraperError]] = {
    401: AuthError,
    403: BlacklistedError,
    404: NotFoundError,
    423: ApiClosedError,
    426: ApiClosedError,
    429: RateLimitError,
    430: QuotaExceededError,
    431: QuotaExceededError,
}


def looks_like_json(body: str) -> bool:
    """True if the body is plausibly a JSON document rather than an error page."""
    return body.lstrip()[:1] in ("{", "[")


def classify(status: int, body: str) -> ScreenScraperError | None:
    """Return the error this response represents, or ``None`` if it succeeded.

    Body markers are checked before status codes, because a French error
    sentence in a ``200`` body is both common and more specific than the status.

    Marker scanning is skipped for JSON bodies.  A successful ``jeuInfos``
    payload can easily contain a phrase like "not found" inside a game synopsis,
    and matching that would turn a good response into a spurious failure — so
    JSON is judged on its status code alone.
    """
    stripped = body.strip()

    if not looks_like_json(stripped):
        text = strip_html(stripped) if stripped[:5].lower() == "<html" else stripped
        folded = _fold(text)
        for marker, error_class in _MARKERS:
            if marker in folded:
                return error_class(text or marker, status=status, body=body)
    else:
        text = stripped

    by_status = _STATUS_MAP.get(status)
    if by_status is not None:
        return by_status(text or f"HTTP {status}", status=status, body=body)

    if status >= 400:
        # Unknown non-2xx: treat as transient and log verbatim rather than
        # crashing on an API contract we have not seen before.
        return TransientError(text or f"HTTP {status}", status=status, body=body)

    return None
