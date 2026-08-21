"""Unit tests for `Settings.cors_origins` (config.py) — the JSON-array/comma-separated
dual-format parser. See config.py::cors_origins docstring for the trailing-slash /
quote-stripping rationale (mirrors secretarIA's CORS_ALLOW_ORIGINS hardening)."""

from brain_api.config import Settings


def _origins(raw: str) -> list[str]:
    return Settings(CORS_ALLOW_ORIGINS=raw).cors_origins


def test_legacy_comma_separated():
    assert _origins("https://a.com,https://b.com") == ["https://a.com", "https://b.com"]


def test_single_origin_comma_form():
    assert _origins("https://a.com") == ["https://a.com"]


def test_json_array():
    assert _origins('["https://a.com", "https://b.com"]') == [
        "https://a.com",
        "https://b.com",
    ]


def test_json_array_single_entry():
    assert _origins('["https://secretaria-secretaria-frontend.cpux9k.easypanel.host"]') == [
        "https://secretaria-secretaria-frontend.cpux9k.easypanel.host"
    ]


def test_trailing_slash_stripped_comma_form():
    assert _origins("https://a.com/,https://b.com/") == ["https://a.com", "https://b.com"]


def test_trailing_slash_stripped_json_form():
    assert _origins('["https://a.com/"]') == ["https://a.com"]


def test_surrounding_quotes_stripped():
    assert _origins("'https://a.com',\"https://b.com\"") == ["https://a.com", "https://b.com"]


def test_blank_and_whitespace_entries_dropped():
    assert _origins("https://a.com, ,https://b.com,") == ["https://a.com", "https://b.com"]


def test_malformed_json_array_fails_closed_not_raising():
    assert _origins('["https://a.com"') == []


def test_empty_string():
    assert _origins("") == []
