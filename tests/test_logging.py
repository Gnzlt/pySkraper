"""Credential redaction.

Passwords travel in ScreenScraper query strings, so any log line carrying a URL
is a potential leak -- including httpx's own connection-error messages and any
traceback that quotes them. Redaction therefore lives in the formatter, and this
file is the assertion that it actually holds.
"""

from __future__ import annotations

import logging

import pytest

from pyskraper.logging_setup import REDACTION, RedactingFormatter, register_secret

SECRET = "sup3rs3cr3t-password"


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_secret(SECRET)


def _format(message: str, *args: object, exc_info: object = None) -> str:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg=message, args=args, exc_info=None
    )
    return RedactingFormatter("%(message)s").format(record)


def test_secret_in_a_plain_message_is_redacted() -> None:
    assert SECRET not in _format(f"logging in with {SECRET}")


def test_secret_in_a_url_is_redacted() -> None:
    url = f"https://api.screenscraper.fr/api2/jeuInfos.php?ssid=me&sspassword={SECRET}&crc=ABC"
    output = _format("GET %s", url)
    assert SECRET not in output
    assert REDACTION in output
    assert "crc=ABC" in output, "only the secret should be removed, not the surrounding context"


def test_secret_in_lazy_format_args_is_redacted() -> None:
    # The formatter scrubs the rendered string, so %s interpolation is covered.
    assert SECRET not in _format("password is %s", SECRET)


def test_secret_inside_a_traceback_is_redacted() -> None:
    """The likeliest real leak: httpx puts the full request URL into connection
    error messages, which then land in a traceback."""
    try:
        raise ConnectionError(f"failed connecting to https://api.screenscraper.fr/?sspassword={SECRET}")
    except ConnectionError:
        import sys

        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1, msg="boom", args=(), exc_info=sys.exc_info()
        )
        output = RedactingFormatter("%(message)s").format(record)

    assert SECRET not in output


def test_short_values_are_not_registered() -> None:
    """Redacting a 2-character value would black out unrelated text everywhere."""
    register_secret("ab")
    assert "ab" in _format("a stable cabbage")


def test_empty_secret_is_ignored() -> None:
    register_secret("")
    register_secret(None)
    assert _format("nothing to hide") == "nothing to hide"
