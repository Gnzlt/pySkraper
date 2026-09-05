"""Turn a ``jeuInfos.php`` game payload into front-end-agnostic metadata.

Everything here is defensive on purpose.  ScreenScraper's schema drifts, fields
appear and vanish per game, and the same field arrives as a list, a bare object
or a string depending on how many values exist.  A scraper that assumes one
shape crashes partway through somebody's library, having already spent the
quota -- so every accessor tolerates every shape and returns ``None`` rather
than raising.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.models import GameMetadata, MediaAsset
from .selectors import select_localized, select_media

__all__ = ["normalize_date", "parse_media", "parse_metadata"]

_YEAR_ONLY = re.compile(r"^\s*(\d{4})\s*$")
_ISO_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_COMPACT = re.compile(r"^\s*(\d{8})T\d{6}\s*$")

# ScreenScraper rates out of 20; EmulationStation gamelists use 0.0-1.0.
_RATING_SCALE = 20.0


def normalize_date(value: str | None) -> str | None:
    """Convert a ScreenScraper date to the EmulationStation ``YYYYMMDDT000000`` form.

    Both ``1991-08-13`` and a bare ``1991`` occur in real data; a year-only date
    becomes January 1st, which is what every other scraper does and what themes
    expect when they render "1991".
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    if _COMPACT.match(text):
        return text

    match = _ISO_DATE.match(text)
    if match:
        return f"{match.group(1)}{match.group(2)}{match.group(3)}T000000"

    match = _YEAR_ONLY.match(text)
    if match:
        return f"{match.group(1)}0101T000000"

    return None


def _text_of(node: Any) -> str | None:
    """Read ``{"text": ...}``, a bare string, or the first of a list of those."""
    if node is None:
        return None
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, Mapping):
        value = node.get("text")
        return str(value).strip() or None if value is not None else None
    if isinstance(node, list):
        for item in node:
            found = _text_of(item)
            if found:
                return found
    return None


def _genre_of(node: Any, chain: Sequence[str | None]) -> str | None:
    """Genres nest a localised name list inside each genre object."""
    if not isinstance(node, list):
        return _text_of(node)
    names: list[str] = []
    for genre in node:
        if not isinstance(genre, Mapping):
            continue
        label = select_localized(genre.get("noms"), chain, field="langue")
        if label and label not in names:
            names.append(label)
    return ", ".join(names) if names else None


def _rating_of(node: Any) -> float | None:
    raw = _text_of(node)
    if raw is None:
        return None
    try:
        score = float(raw)
    except ValueError:
        return None
    if score < 0:
        # ScreenScraper uses -1 for "no rating"; writing 0.0 would claim the
        # game was rated terribly rather than not rated at all.
        return None
    return round(min(1.0, score / _RATING_SCALE), 4)


def _players_of(node: Any) -> str | None:
    raw = _text_of(node)
    if raw is None:
        return None
    # "1-2" and "2" both occur; keep the string, since gamelists accept both.
    return raw.strip() or None


def parse_metadata(
    game: Mapping[str, Any],
    *,
    region_chain: Sequence[str | None],
    language_chain: Sequence[str | None],
    fallback_name: str,
) -> GameMetadata:
    """Build :class:`GameMetadata` from one ``jeu`` object."""
    name = select_localized(game.get("noms"), region_chain) or fallback_name

    game_id: int | None = None
    raw_id = game.get("id")
    if raw_id is not None:
        try:
            game_id = int(str(raw_id))
        except ValueError:
            game_id = None

    return GameMetadata(
        name=name,
        ss_game_id=game_id,
        description=select_localized(game.get("synopsis"), language_chain, field="langue"),
        genre=_genre_of(game.get("genres"), language_chain),
        developer=_text_of(game.get("developpeur")),
        publisher=_text_of(game.get("editeur")),
        release_date=normalize_date(select_localized(game.get("dates"), region_chain)),
        players=_players_of(game.get("joueurs")),
        rating=_rating_of(game.get("note")),
        region=_first_field(game.get("rom"), "romregions"),
        language=_first_field(game.get("rom"), "romlangues"),
        family=select_localized(game.get("familles"), language_chain, field="langue"),
        arcade_system=_text_of(game.get("systeme")),
    )


def _first_field(rom_node: Any, key: str) -> str | None:
    """Read the first entry of a comma-separated rom attribute.

    ScreenScraper packs multiple regions or languages into one string
    ("us,eu,jp"); gamelists want a single value.
    """
    if isinstance(rom_node, Mapping):
        value = rom_node.get(key)
        if value:
            return str(value).strip().split(",")[0].strip() or None
    return None


def parse_media(
    game: Mapping[str, Any],
    *,
    media_config: Mapping[str, Sequence[str]],
    region_chain: Sequence[str | None],
) -> list[MediaAsset]:
    """Resolve every configured tag to a concrete asset, skipping the empty ones."""
    medias = game.get("medias")
    if not isinstance(medias, list):
        return []

    entries = [m for m in medias if isinstance(m, Mapping)]
    assets: list[MediaAsset] = []

    for tag, keys in media_config.items():
        if not keys:
            continue
        chosen = select_media(entries, list(keys), region_chain)
        if chosen is None:
            continue
        url = chosen.get("url")
        if not url:
            continue

        assets.append(
            MediaAsset(
                tag=tag,
                key=str(chosen.get("type") or ""),
                url=str(url),
                region=(str(chosen["region"]).strip() or None) if chosen.get("region") else None,
                fmt=(str(chosen["format"]).strip().lower() or None) if chosen.get("format") else None,
                expected_size=_int_or_none(chosen.get("size")),
                md5=(str(chosen["md5"]).strip() or None) if chosen.get("md5") else None,
            )
        )

    return assets


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
