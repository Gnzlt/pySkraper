"""The palette.

Rich resolves a markup name when the line is printed, not when it is written,
so a style the theme does not define is a crash in front of the user at the
exact moment they were being asked a question.  The check below is the cheap
version of noticing that: every name the wizard prints has to resolve.
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, Task

from pyskraper import wizard
from pyskraper.theme import PINK, WIZARD, YELLOW, SweepBarColumn, blend, rgb

# Markup as it appears in a string literal.  Reading the literals rather than
# the whole file matters: `groups[vendor]` and `[str]` are subscripts, not
# styles, and a plain grep over the source picks them up as both.
_MARKUP = re.compile(r"\[([a-z][a-z0-9. ]*)\]")


def _styles_printed_by(module: object) -> set[str]:
    source = Path(module.__file__).read_text()  # type: ignore[attr-defined]
    tree = ast.parse(source)
    literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    return {name for literal in literals for name in _MARKUP.findall(literal)}


def test_every_style_the_wizard_prints_resolves() -> None:
    console = Console(theme=WIZARD)

    printed = _styles_printed_by(wizard)
    assert printed, "found no markup at all -- the extraction is broken, not the theme"
    for name in sorted(printed):
        console.get_style(name)  # raises MissingStyle if the theme forgot it


def test_the_wizard_console_carries_the_theme() -> None:
    assert wizard.console.get_style("step") == Console(theme=WIZARD).get_style("step")


def test_rgb_splits_a_hex_colour_into_channels() -> None:
    assert rgb("#ffd479") == (255, 212, 121)
    assert rgb("#000000") == (0, 0, 0)


def _task(completed: int, total: float | None) -> Task:
    """A real Task -- building one by hand means keeping up with rich's fields."""
    progress = Progress(SweepBarColumn(), console=Console(file=io.StringIO()))
    progress.add_task("scraping", total=total, completed=completed)
    return progress.tasks[0]


def _cell_styles(completed: int, total: float | None = 100) -> list[str]:
    bar = SweepBarColumn(bar_width=10).render(_task(completed, total))
    return [str(span.style) for span in bar.spans]


def test_an_empty_bar_is_all_track() -> None:
    assert _cell_styles(0) == ["bar.back"] * 10


def test_a_full_bar_sweeps_from_yellow_to_pink() -> None:
    styles = _cell_styles(100)

    assert "bar.back" not in styles
    assert styles[0] == YELLOW
    assert styles[-1] == PINK


def test_a_half_full_bar_keeps_the_colours_it_already_had() -> None:
    """The sweep is fixed to the bar, not stretched over the filled part."""
    half, full = _cell_styles(50), _cell_styles(100)

    assert half[:5] == full[:5]
    assert half[5:] == ["bar.back"] * 5


def test_a_bar_with_no_total_does_not_divide_by_it() -> None:
    assert _cell_styles(7, total=None) == ["bar.back"] * 10


def test_blend_walks_between_the_two_ends() -> None:
    assert blend(YELLOW, PINK, 0.0) == YELLOW
    assert blend(YELLOW, PINK, 1.0) == PINK
    assert rgb(blend(YELLOW, PINK, 0.5)) == (255, 146, 142)
