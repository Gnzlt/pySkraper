"""The splash: the pySkraper wordmark, drawn in block characters.

It is decoration, which also makes it the first thing to go: the art prints
only when a terminal is there to receive it, wide enough for all 53 columns,
with an encoding that can carry the block characters.  Anywhere else -- a pipe,
a log file, a cron job, a terminal on a legacy encoding -- the same information
arrives as one plain line, so nothing downstream ever has to cope with a
picture.

The colours are sampled from the logo: the same yellow-to-pink sweep it uses,
run left to right across the letters.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from . import __version__
from .theme import PINK, YELLOW, blend

__all__ = ["WIDTH", "print_banner"]

TAGLINE = "ScreenScraper for KNULLI/Batocera handhelds"

#: Columns the art occupies.  Narrower terminals get the plain line instead.
WIDTH = 53

_WORDMARK = (
    "             ████ █",
    "████  █   █ █     █  ██ █ ███  ████ ████   ███  █ ███",
    "█   █ █   █  ███  █ ██  ██        █ █   █ █████ ██",
    "█   █ █   █     █ ███   █     █   █ █   █ █     █",
    "████   ████ ████  █  ██ █      ████ ████   ███  █",
    "█     ████                          █",
)

# The sweep is drawn in chunks rather than per character: a character at a time
# would triple the size of the escape sequence for a difference no eye catches.
_SWEEP_STEPS = 12


def _sweep(line: str) -> Text:
    """One line coloured left to right, positioned against the full banner width."""
    text = Text()
    step = max(1, WIDTH // _SWEEP_STEPS)
    for start in range(0, len(line), step):
        amount = min(1.0, start / (WIDTH - 1))
        text.append(line[start : start + step], style=blend(YELLOW, PINK, amount))
    return text


def _can_draw(console: Console) -> bool:
    """Whether this console can show the art without mangling it.

    The encoding check is not paranoia: a terminal running under ``LC_ALL=C``
    turns every block character into a question mark, and rich raises rather
    than substituting when it cannot encode what it was given.
    """
    if not console.is_terminal or console.width < WIDTH:
        return False
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    try:
        "".join(_WORDMARK).encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def print_banner(console: Console) -> None:
    """Print the wordmark, or the one-line version of it where the art will not fit."""
    if not _can_draw(console):
        # Deliberately plain ASCII, em dash included: this is the branch taken
        # when the terminal could not encode the art, and a fallback that also
        # needs UTF-8 is not a fallback.
        console.print(f"[bold]pySkraper[/] {__version__} - {TAGLINE}", highlight=False)
        return

    console.print()
    for line in _WORDMARK:
        console.print(_sweep(line))
    console.print(f"\n[dim]{TAGLINE} · v{__version__}[/]", highlight=False)
