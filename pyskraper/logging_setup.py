"""Logging with credential redaction built into the formatter.

Redaction lives in the formatter, not at call sites, for one reason: there is
exactly one place to get it right.  Any code path that logs a URL, an exception
message, or an httpx request repr would otherwise be one forgotten ``%s`` away
from writing a password to disk -- and passwords travel in ScreenScraper query
strings, so that risk is everywhere rather than in one obvious spot.

``tests/test_logging.py`` asserts that no registered secret survives formatting,
at any level, through any of those paths.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

__all__ = ["REDACTION", "RedactingFormatter", "register_secret", "setup_logging"]

REDACTION = "***"

# Module-level rather than per-formatter: secrets are registered as config
# loads, which happens before handlers are necessarily built.
_SECRETS: set[str] = set()

# Short values would redact harmless substrings all over the output.
_MIN_SECRET_LENGTH = 4


def register_secret(value: str | None) -> None:
    """Mark a value as never-loggable."""
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """Replace every registered secret in ``text``."""
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, REDACTION)
    return text


class RedactingFormatter(logging.Formatter):
    """A formatter that scrubs registered secrets from the final string.

    Scrubbing the *formatted* output rather than the arguments means it also
    covers tracebacks, which is where a leaked password is most likely to end
    up: httpx puts the full request URL into connection error messages.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(verbosity: int = 0, *, log_file: Path | None = None) -> None:
    """Configure the root logger.  ``verbosity`` is the count of ``-v`` flags."""
    level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(min(level, logging.INFO if log_file else level))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(RedactingFormatter("%(message)s"))
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(file_handler)
        root.setLevel(logging.DEBUG)

    # httpx logs every request URL at INFO -- including our credentials.  The
    # formatter would redact them, but there is no reason to write them at all.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
