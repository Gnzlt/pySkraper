"""The palette: the logo's colours, reused everywhere pySkraper prints.

The logo is a yellow-to-pink sweep, and the wizard borrows it so the tool reads
as one thing rather than two.  The rules are narrow on purpose:

* Hue carries a meaning, it is not decoration.  Pink is "this is yours to
  press", yellow is "this is the value we settled on", dim is context you can
  skip on the way past.
* The three states that have to survive a glance -- it worked, take care, it
  broke -- keep their conventional green/amber/red readings.  They are tinted
  to sit beside the rest rather than fight it, and no further.
* None of it is load-bearing.  rich drops what a terminal cannot render, so
  eight colours, ``NO_COLOR=1``, and a pipe into ``less`` all still get exactly
  the same words in the same order.
"""

from __future__ import annotations

from rich.progress import ProgressColumn, Task
from rich.table import Column
from rich.text import Text
from rich.theme import Theme

__all__ = ["AMBER", "MINT", "PINK", "RED", "WIZARD", "YELLOW", "SweepBarColumn", "blend", "rgb"]

#: Sampled off the logo: the two ends of its sweep, and the shades between.
YELLOW = "#ffd479"
AMBER = "#f7b55d"
PINK = "#ff4fa3"

#: Off the palette, and deliberately: green and red have to keep meaning
#: something even in a terminal full of pink.
MINT = "#5ef1a4"
RED = "#ff5f7a"


def rgb(colour: str) -> tuple[int, int, int]:
    """``"#ffd479"`` as the three channels, for code that interpolates."""
    value = colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def blend(start: str, end: str, amount: float) -> str:
    """The colour ``amount`` of the way from ``start`` to ``end``, as hex."""
    red, green, blue = (round(a + (b - a) * amount) for a, b in zip(rgb(start), rgb(end), strict=True))
    return f"#{red:02x}{green:02x}{blue:02x}"


WIZARD = Theme(
    {
        "step": f"bold {PINK}",  # the step number
        "title": f"bold {YELLOW}",  # what the step is called
        "key": f"bold {AMBER}",  # something to type: an index, a letter
        "cmd": f"bold {PINK}",  # a command to run afterwards
        "marker": f"bold {PINK}",  # the > or x against a chosen row
        "value": YELLOW,  # a setting, as it currently stands
        "label": f"dim {AMBER}",  # a table heading, a row label
        "url": f"underline {AMBER}",  # somewhere to go and sign up
        "ok": MINT,
        "warn": AMBER,
        "danger": f"bold {RED}",
        # rich's own prompt styles, so the "[y/n] (n):" tail matches the rest.
        "prompt.choices": AMBER,
        "prompt.default": PINK,
        "prompt.invalid": RED,
    }
)


class SweepBarColumn(ProgressColumn):
    """A progress bar filled with the logo's sweep rather than one flat colour.

    rich's own ``BarColumn`` takes a single style for the completed part, so the
    gradient has to be drawn a cell at a time and the bar rendered here.  A
    cell's colour comes from where it sits in the bar, never from how full the
    bar is: the sweep stays still and the fill moves across it, which is what
    makes it read as one image rather than a colour that drifts while you watch.

    The unfilled track is left as rich's own ``bar.back`` -- whatever a terminal
    already renders behind a progress bar is the right answer here too.
    """

    #: What rich's own bar is drawn with, so the two look like siblings.
    CELL = "\u2501"

    def __init__(self, bar_width: int = 40, table_column: Column | None = None) -> None:
        self.bar_width = bar_width
        super().__init__(table_column=table_column)

    def render(self, task: Task) -> Text:
        width = max(self.bar_width, 1)
        fraction = min(1.0, task.completed / task.total) if task.total else 0.0
        filled = int(width * fraction)

        bar = Text(no_wrap=True)
        for cell in range(width):
            style = blend(YELLOW, PINK, cell / (width - 1)) if width > 1 else YELLOW
            bar.append(self.CELL, style=style if cell < filled else "bar.back")
        return bar
