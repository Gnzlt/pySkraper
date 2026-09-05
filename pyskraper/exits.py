"""Process exit codes.

These are a contract with whatever calls the tool -- a cron job or a CI step
branches on them -- so they live in one place.  They used to be named in
``cli.py`` and again in ``wizard.py``, with neither module aware of the other's
spelling, and written as bare literals in ``core/scraper.py``.  That is how two
vocabularies drift apart.

Do not renumber these, and do not add one without a reason a caller can act on.
"""

from __future__ import annotations

__all__ = ["EXIT_CONFIG", "EXIT_INTERRUPTED", "EXIT_OK", "EXIT_PARTIAL", "EXIT_QUOTA"]

EXIT_OK = 0
"""Everything asked for was done."""

EXIT_PARTIAL = 1
"""Ran, but something did not match or did not download."""

EXIT_CONFIG = 2
"""Refused to start: bad configuration, missing credentials, no card."""

EXIT_QUOTA = 3
"""Stopped early because the daily ScreenScraper allowance ran out."""

EXIT_INTERRUPTED = 130
"""Ctrl-C.  The shell convention: 128 + SIGINT."""
