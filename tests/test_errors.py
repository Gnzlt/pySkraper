"""The error classifier is the gate every response passes through, so its
edge cases matter more than most: a misclassification either aborts a healthy
run or keeps hammering an API that has told us to stop."""

from __future__ import annotations

import pytest

from pyskraper.api.errors import (
    ApiClosedError,
    AuthError,
    BlacklistedError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    TransientError,
    classify,
    strip_html,
)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, "Erreur de login : Vérifier vos identifiants", AuthError),
        (403, "Votre softname est blacklisté", BlacklistedError),
        (404, "Erreur : Rom/Iso/Dossier non trouvée !", NotFoundError),
        (423, "API totalement fermée", ApiClosedError),
        (426, "API fermée pour les non membres", ApiClosedError),
        (429, "maximum threads per minute reached", RateLimitError),
        (430, "Votre quota de scrape est dépassé pour aujourd'hui", QuotaExceededError),
        (431, "Le quota de scrape est dépassé", QuotaExceededError),
        (500, "Internal Server Error", TransientError),
        (503, "", TransientError),
    ],
)
def test_classifies_each_documented_failure(status: int, body: str, expected: type[Exception]) -> None:
    error = classify(status, body)
    assert isinstance(error, expected)


def test_accented_and_unaccented_spellings_both_match() -> None:
    # "non trouvée" and "non trouvé" both appear in real responses.
    assert isinstance(classify(200, "Rom non trouvée !"), NotFoundError)
    assert isinstance(classify(200, "Rom non trouve"), NotFoundError)


def test_french_message_in_a_200_body_beats_the_status_code() -> None:
    # This is the whole reason markers are checked before status codes.
    error = classify(200, "Erreur de login : mot de passe incorrect")
    assert isinstance(error, AuthError)


def test_json_success_body_is_never_marker_matched() -> None:
    """A game synopsis containing an error-ish phrase must not fail the run.

    Regression guard: scanning marker text across JSON bodies would turn a
    perfectly good response into a spurious NotFoundError, and the game whose
    description mentions a lost artifact would silently never scrape.
    """
    body = '{"response": {"jeu": {"synopsis": [{"text": "The treasure was non trouve for centuries."}]}}}'
    assert classify(200, body) is None


def test_success_returns_none() -> None:
    assert classify(200, '{"response": {"ssuser": {}}}') is None


def test_html_error_page_is_reduced_to_text() -> None:
    body = "<html><body><h1>Erreur de login</h1><p>Bad&nbsp;credentials</p></body></html>"
    error = classify(200, body)
    assert isinstance(error, AuthError)
    assert "<" not in str(error)


def test_strip_html_collapses_whitespace() -> None:
    assert strip_html("<p>one</p>\n\n<p>two</p>") == "one two"


def test_retry_and_abort_semantics_are_explicit() -> None:
    assert RateLimitError("x").retryable and not RateLimitError("x").fatal
    assert TransientError("x").retryable
    assert AuthError("x").fatal and not AuthError("x").retryable
    assert QuotaExceededError("x").fatal
    # A miss is neither fatal nor retryable: the run continues, but it cost
    # KO quota, so the caller has to account for it.
    assert not NotFoundError("x").fatal and not NotFoundError("x").retryable
