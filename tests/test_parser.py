"""Parsing jeuInfos payloads.

Every accessor here is defensive because ScreenScraper's shapes vary per game:
the same field arrives as a list, a bare object, or a string depending on how
many values exist. A parser that assumes one shape crashes partway through a
library, after the quota has already been spent.
"""

from __future__ import annotations

from typing import Any

from pyskraper.api.parser import normalize_date, parse_media, parse_metadata
from pyskraper.api.selectors import build_region_chain

REGION = build_region_chain("en", "us", ["wor", "eu", "jp"])
LANGUAGE = build_region_chain("en", None, ["en", "wor"])

GAME: dict[str, Any] = {
    "id": "1234",
    "noms": [{"region": "wor", "text": "Super Mario World"}, {"region": "jp", "text": "Super Mario World JP"}],
    "synopsis": [{"langue": "en", "text": "Mario travels through Dinosaur Land."}],
    "genres": [{"noms": [{"langue": "en", "text": "Platform"}]}, {"noms": [{"langue": "en", "text": "Action"}]}],
    "editeur": {"text": "Nintendo"},
    "developpeur": {"text": "Nintendo EAD"},
    "dates": [{"region": "us", "text": "1991-08-13"}],
    "joueurs": {"text": "2"},
    "note": {"text": "18"},
    "medias": [
        {"type": "ss", "region": "us", "url": "http://x/ss.png", "format": "png", "size": "12345"},
        {"type": "box-2D", "region": "wor", "url": "http://x/box.png", "format": "png"},
    ],
}


def test_extracts_core_metadata() -> None:
    meta = parse_metadata(GAME, region_chain=REGION, language_chain=LANGUAGE, fallback_name="fallback")
    assert meta.name == "Super Mario World"
    assert meta.ss_game_id == 1234
    assert meta.developer == "Nintendo EAD"
    assert meta.publisher == "Nintendo"
    assert meta.players == "2"
    assert meta.description is not None and "Dinosaur Land" in meta.description


def test_rating_is_rescaled_from_20_to_1() -> None:
    meta = parse_metadata(GAME, region_chain=REGION, language_chain=LANGUAGE, fallback_name="x")
    assert meta.rating == 0.9


def test_absent_rating_is_none_not_zero() -> None:
    """ScreenScraper uses -1 for 'not rated'. Writing 0.0 would claim the game
    was rated terribly rather than not rated at all."""
    game = dict(GAME, note={"text": "-1"})
    meta = parse_metadata(game, region_chain=REGION, language_chain=LANGUAGE, fallback_name="x")
    assert meta.rating is None


def test_multiple_genres_are_joined() -> None:
    meta = parse_metadata(GAME, region_chain=REGION, language_chain=LANGUAGE, fallback_name="x")
    assert meta.genre == "Platform, Action"


def test_fallback_name_used_when_the_payload_has_none() -> None:
    meta = parse_metadata({}, region_chain=REGION, language_chain=LANGUAGE, fallback_name="My ROM")
    assert meta.name == "My ROM"


def test_empty_payload_does_not_raise() -> None:
    meta = parse_metadata({}, region_chain=REGION, language_chain=LANGUAGE, fallback_name="x")
    assert meta.rating is None and meta.genre is None and meta.release_date is None


def test_non_numeric_game_id_is_tolerated() -> None:
    meta = parse_metadata({"id": "not-a-number"}, region_chain=REGION, language_chain=LANGUAGE, fallback_name="x")
    assert meta.ss_game_id is None


class TestDates:
    def test_iso_date(self) -> None:
        assert normalize_date("1991-08-13") == "19910813T000000"

    def test_bare_year_becomes_january_first(self) -> None:
        # Both forms occur in real data; themes render the year either way.
        assert normalize_date("1991") == "19910101T000000"

    def test_already_compact_is_left_alone(self) -> None:
        assert normalize_date("19910813T000000") == "19910813T000000"

    def test_garbage_is_dropped_rather_than_guessed(self) -> None:
        assert normalize_date("sometime in the 90s") is None
        assert normalize_date("") is None
        assert normalize_date(None) is None


def test_media_resolution_respects_config_and_regions() -> None:
    assets = parse_media(GAME, media_config={"image": ["ss"], "thumbnail": ["box-2D"]}, region_chain=REGION)
    by_tag = {a.tag: a for a in assets}
    assert by_tag["image"].url == "http://x/ss.png"
    assert by_tag["image"].expected_size == 12345
    assert by_tag["image"].extension == ".png"
    assert by_tag["thumbnail"].url == "http://x/box.png"


def test_disabled_tags_produce_no_asset() -> None:
    assets = parse_media(GAME, media_config={"image": ["ss"], "manual": []}, region_chain=REGION)
    assert {a.tag for a in assets} == {"image"}


def test_missing_media_block_is_not_an_error() -> None:
    assert parse_media({}, media_config={"image": ["ss"]}, region_chain=REGION) == []


def test_asset_without_a_url_is_skipped() -> None:
    game = {"medias": [{"type": "ss", "region": "us"}]}
    assert parse_media(game, media_config={"image": ["ss"]}, region_chain=REGION) == []
