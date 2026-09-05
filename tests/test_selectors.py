"""Region and key fallback -- the logic that decides which asset a card gets."""

from __future__ import annotations

from pyskraper.api.selectors import build_region_chain, select_localized, select_media


def test_chain_matches_the_documented_order() -> None:
    chain = build_region_chain("en", "us", ["wor", "us", "eu", "jp", "ss", "cus"])
    assert chain == ["en", "us", "wor", "eu", "jp", "ss", "cus", None]


def test_chain_ends_with_none_for_unregioned_assets() -> None:
    # Plenty of media carry no region at all; dropping them would leave tags
    # empty for no reason.
    assert build_region_chain("en", "us", [])[-1] is None


def test_chain_deduplicates_without_reordering() -> None:
    chain = build_region_chain("us", "us", ["us", "wor"])
    assert chain == ["us", "wor", None]


MEDIAS = [
    {"type": "ss", "region": "jp", "url": "ss-jp", "format": "png"},
    {"type": "ss", "region": "us", "url": "ss-us", "format": "png"},
    {"type": "box-2D", "region": "wor", "url": "box-wor", "format": "png"},
    {"type": "wheel", "url": "wheel-noregion", "format": "png"},
]


def test_preferred_region_wins() -> None:
    chosen = select_media(MEDIAS, ["ss"], build_region_chain("en", "us", ["wor", "jp"]))
    assert chosen is not None and chosen["url"] == "ss-us"


def test_falls_back_through_the_chain() -> None:
    chosen = select_media(MEDIAS, ["ss"], build_region_chain(None, "eu", ["jp"]))
    assert chosen is not None and chosen["url"] == "ss-jp"


def test_unregioned_asset_is_reachable() -> None:
    chosen = select_media(MEDIAS, ["wheel"], build_region_chain(None, "us", ["wor"]))
    assert chosen is not None and chosen["url"] == "wheel-noregion"


def test_key_order_beats_region_order() -> None:
    """'First available key wins' means a box-2D in a less-preferred region
    beats falling through to the next key in the list."""
    chosen = select_media(MEDIAS, ["box-2D", "ss"], build_region_chain(None, "us", ["wor"]))
    assert chosen is not None and chosen["url"] == "box-wor"


def test_asset_outside_the_chain_still_beats_an_empty_tag() -> None:
    """If a key exists only in a region nobody listed, use it anyway.

    Returning nothing would leave the tag blank on the card purely because the
    fallback list was short, which is worse than a Korean box scan.
    """
    medias = [{"type": "box-2D", "region": "kr", "url": "box-kr", "format": "png"}]
    chosen = select_media(medias, ["box-2D"], build_region_chain(None, "us", ["wor"]))
    assert chosen is not None and chosen["url"] == "box-kr"


def test_missing_key_returns_none() -> None:
    assert select_media(MEDIAS, ["mixrbv2"], build_region_chain(None, "us", [])) is None


def test_empty_key_list_means_disabled() -> None:
    assert select_media(MEDIAS, [], build_region_chain(None, "us", [])) is None


def test_localized_handles_every_shape_the_api_sends() -> None:
    chain = build_region_chain("en", "us", ["wor"])
    assert select_localized([{"region": "us", "text": "Name US"}], chain) == "Name US"
    assert select_localized({"text": "Bare object"}, chain) == "Bare object"
    assert select_localized("bare string", chain) == "bare string"
    assert select_localized(None, chain) is None


def test_localized_falls_back_to_any_value_rather_than_nothing() -> None:
    # A name in an unexpected language beats no name at all.
    chain = build_region_chain("en", "us", [])
    assert select_localized([{"region": "kr", "text": "한국어"}], chain) == "한국어"
