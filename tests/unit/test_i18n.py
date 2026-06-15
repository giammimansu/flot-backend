"""Flot — i18n catalog + resolver tests."""
from __future__ import annotations

import pytest

from lib.i18n import DEFAULT, MESSAGES, SUPPORTED, normalize_lang, tr, tr_user, user_lang


@pytest.mark.parametrize("value,expected", [
    ("it", "it"),
    ("en", "en"),
    ("fr", "en"),
    ("de", "en"),
    ("es", "en"),
    (None, "en"),
    ("", "en"),
    ("IT", "en"),  # case-sensitive: only exact 'it'
])
def test_normalize_lang(value, expected):
    assert normalize_lang(value) == expected


def test_user_lang():
    assert user_lang({"lang": "it"}) == "it"
    assert user_lang({"lang": "de"}) == "en"
    assert user_lang({}) == "en"
    assert user_lang(None) == "en"


def test_tr_interpolates():
    out = tr("partner_unlocked.body", "it", savings="5")
    assert "~€5" in out
    out_en = tr("partner_unlocked.body", "en", savings="5")
    assert "~€5" in out_en and out_en != out


def test_tr_distinct_languages():
    assert tr("match_found.title", "it") == "Match trovato! 🎉"
    assert tr("match_found.title", "en") == "Match found! 🎉"


def test_tr_unknown_lang_falls_back_to_default():
    # 'fr' normalizes to 'en'
    assert tr("match_found.title", "fr") == tr("match_found.title", DEFAULT)


def test_tr_missing_key_returns_key():
    assert tr("does.not.exist", "it") == "does.not.exist"


def test_tr_missing_kwarg_does_not_crash():
    # savings not provided → placeholder preserved, no exception
    out = tr("partner_unlocked.body", "en")
    assert "{savings}" in out


def test_tr_user_uses_profile_lang():
    assert tr_user("match_found.title", {"lang": "it"}) == "Match trovato! 🎉"
    assert tr_user("match_found.title", {"lang": "es"}) == "Match found! 🎉"


def test_catalog_complete_it_en():
    """Every catalog entry must define both 'it' and 'en'."""
    for key, langs in MESSAGES.items():
        for lang in SUPPORTED:
            assert langs.get(lang), f"{key} missing {lang}"
