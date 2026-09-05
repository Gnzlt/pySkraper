"""The splash screen.

The banner is the one piece of output that exists only to look nice, so the
tests here are about it knowing when to get out of the way: a pipe, a narrow
window, or a terminal that cannot encode a block character all have to end up
with the plain line instead of a mangled picture.
"""

from __future__ import annotations

import io
import re

from rich.console import Console

from pyskraper import __version__
from pyskraper.banner import _WORDMARK, TAGLINE, WIDTH, print_banner

BLOCKS = "█▀▄"
PLAIN_LINE = "pySkraper {version} - {tagline}"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Escape sequences out, whitespace collapsed -- rich still emits bold under no_color."""
    return " ".join(_ANSI.sub("", text).split())


def _render(*, terminal: bool, width: int) -> str:
    stream = io.StringIO()
    print_banner(Console(file=stream, force_terminal=terminal, no_color=True, width=width))
    return stream.getvalue()


def test_no_art_line_is_wider_than_the_declared_width() -> None:
    """WIDTH is what the fallback decision is made on -- it has to be true."""
    for line in _WORDMARK:
        assert len(line) <= WIDTH


def test_a_wide_terminal_gets_the_art() -> None:
    out = _render(terminal=True, width=100)

    assert any(char in out for char in BLOCKS)
    assert TAGLINE in out


def test_a_pipe_gets_one_plain_line() -> None:
    out = _render(terminal=False, width=100)

    assert not any(char in out for char in BLOCKS)
    assert _plain(out) == PLAIN_LINE.format(version=__version__, tagline=TAGLINE)


def test_a_narrow_terminal_gets_one_plain_line() -> None:
    out = _render(terminal=True, width=WIDTH - 1)

    assert not any(char in out for char in BLOCKS)
    assert _plain(out) == PLAIN_LINE.format(version=__version__, tagline=TAGLINE)


def test_an_encoding_that_cannot_carry_blocks_gets_one_plain_line() -> None:
    """A terminal under LC_ALL=C: rich raises rather than substituting."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    console = Console(file=stream, force_terminal=True, no_color=True, width=100)

    print_banner(console)
    stream.flush()

    written = stream.buffer.getvalue().decode("ascii")
    assert not any(char in written for char in BLOCKS)
    assert _plain(written) == PLAIN_LINE.format(version=__version__, tagline=TAGLINE)


def test_the_art_is_coloured_when_colour_is_available() -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system="truecolor", width=100)

    print_banner(console)

    assert "\x1b[38;2;" in stream.getvalue()
