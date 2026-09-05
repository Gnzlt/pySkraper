"""Choosing which asset and which localisation to use.

The region fallback order is not ours to invent -- it mirrors what
``batocera-emulationstation`` does in ``ScreenScraper.cpp``'s ``findMedia``, so
that a card scraped by this tool looks the same as one scraped by the front-end
itself::

    language -> region -> wor -> us -> eu -> jp -> ss -> cus -> (unset)

The trailing unset entry matters: plenty of assets carry no region at all, and
dropping them would leave tags empty for no reason.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = ["build_region_chain", "select_localized", "select_media"]

DEFAULT_FALLBACK: tuple[str, ...] = ("wor", "us", "eu", "jp", "ss", "cus")


def build_region_chain(
    language: str | None,
    region: str | None,
    fallback: Sequence[str] | None = None,
) -> list[str | None]:
    """Ordered region preferences, ending with ``None`` for unregioned assets."""
    chain: list[str | None] = []
    for candidate in (language, region, *(fallback if fallback is not None else DEFAULT_FALLBACK)):
        if candidate:
            value = candidate.strip().lower()
            if value and value not in chain:
                chain.append(value)
    chain.append(None)
    return chain


def _entry_region(entry: Mapping[str, Any], field: str) -> str | None:
    value = entry.get(field)
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def select_media(
    medias: Iterable[Mapping[str, Any]],
    keys: Sequence[str],
    chain: Sequence[str | None],
) -> Mapping[str, Any] | None:
    """Pick one asset.

    Key order is the outer loop: the config lists keys in preference order, and
    "first available key wins" means a ``box-2D`` in an unloved region still
    beats falling through to ``box-3D``.
    """
    if not keys:
        return None

    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for entry in medias:
        media_type = entry.get("type")
        if isinstance(media_type, str):
            by_key.setdefault(media_type, []).append(entry)

    for key in keys:
        candidates = by_key.get(key)
        if not candidates:
            continue
        for wanted in chain:
            for entry in candidates:
                if _entry_region(entry, "region") == wanted:
                    return entry

    # Last resort: an asset whose region is not in the chain at all still beats
    # an empty tag on the card.  Key order is preserved, so this only ever
    # fires once every listed key has failed every listed region.
    for key in keys:
        for entry in by_key.get(key, []):
            if entry.get("url"):
                return entry
    return None


def select_localized(
    entries: Any,
    chain: Sequence[str | None],
    *,
    field: str = "region",
    text_field: str = "text",
) -> str | None:
    """Pick the best localisation of a repeated text field (names, dates, ...).

    ScreenScraper sometimes sends these as a list of ``{region, text}`` objects
    and sometimes -- when there is only one -- as a bare object or string.  All
    three shapes appear in real responses, so all three are handled here rather
    than at each of the dozen call sites.
    """
    if entries is None:
        return None
    if isinstance(entries, str):
        return entries.strip() or None
    if isinstance(entries, Mapping):
        value = entries.get(text_field)
        return str(value).strip() or None if value is not None else None
    if not isinstance(entries, list):
        return None

    normalized = [e for e in entries if isinstance(e, Mapping)]
    for wanted in chain:
        for entry in normalized:
            if _entry_region(entry, field) == wanted:
                value = entry.get(text_field)
                if value is not None and str(value).strip():
                    return str(value).strip()

    # Nothing in the chain matched: rather than return nothing, take the first
    # non-empty value.  A name in an unexpected language beats no name at all.
    for entry in normalized:
        value = entry.get(text_field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None
