"""Flot — server-side i18n for notifications (push / email / in-app feed).

All user-facing notification copy lives in MESSAGES, keyed by
`message_key -> lang -> template`. Templates use `str.format(**kwargs)` for
interpolation. Resolution falls back DEFAULT lang, then to the key itself.

Supported langs: only "it" and "en". Any other value (incl. None / "") maps
to DEFAULT ("en"). The user profile carries `lang` (enum it/en/fr/de/es); we
treat anything != "it" as "en".
"""
from __future__ import annotations

from aws_lambda_powertools import Logger

logger = Logger()

SUPPORTED = ("it", "en")
DEFAULT = "en"


def normalize_lang(value: str | None) -> str:
    """'it' stays 'it'; everything else (None, '', 'fr', 'de', 'es') → 'en'."""
    return "it" if value == "it" else DEFAULT


def user_lang(user: dict | None) -> str:
    """Recipient language from a user profile dict."""
    if not user:
        return DEFAULT
    return normalize_lang(user.get("lang"))


class _SafeDict(dict):
    """Missing kwargs render as their literal placeholder instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def tr(key: str, lang: str, /, **kwargs) -> str:
    """Resolve `key` in `lang`, fall back to DEFAULT then to the key itself.

    Interpolation is fail-safe: a missing kwarg keeps its `{placeholder}`
    rather than raising, and any other formatting error returns the raw
    template. Both cases are logged.
    """
    lang = normalize_lang(lang)
    entry = MESSAGES.get(key)
    if entry is None:
        logger.warning("i18n_missing_key", key=key)
        return key

    template = entry.get(lang) or entry.get(DEFAULT)
    if template is None:
        logger.warning("i18n_missing_lang", key=key, lang=lang)
        return key

    try:
        return template.format_map(_SafeDict(kwargs))
    except Exception as e:  # malformed template etc. — never crash a notification
        logger.warning("i18n_format_failed", key=key, lang=lang, error=str(e))
        return template


def tr_user(key: str, user: dict | None, /, **kwargs) -> str:
    """`tr` using the recipient profile's language."""
    return tr(key, user_lang(user), **kwargs)


# ── Message catalog ──────────────────────────────────────────────────
# Keys grouped by event. `.title`/`.body` per channel-agnostic copy;
# `.email.subject`/`.email.body` where email diverges from push.

MESSAGES: dict[str, dict[str, str]] = {
    # match.found
    "match_found.title": {
        "it": "Match trovato! 🎉",
        "en": "Match found! 🎉",
    },
    "match_found.body": {
        "it": "Abbiamo trovato un partner per il tuo viaggio. Sblocca per chattare!",
        "en": "We found a partner for your trip. Unlock to start chatting!",
    },

    # match.partially_unlocked → partner notified
    "partner_unlocked.title": {
        "it": "{name} ha sbloccato! 🔓",
        "en": "{name} unlocked! 🔓",
    },
    "partner_unlocked.body": {
        "it": "Sblocca anche tu per condividere il taxi e risparmiare ~€{savings}",
        "en": "Unlock too to share the taxi and save ~€{savings}",
    },

    # unlock reminders — escalating tone
    "unlock_reminder.first.title": {
        "it": "{partner_name} ti sta aspettando",
        "en": "{partner_name} is waiting for you",
    },
    "unlock_reminder.first.body": {
        "it": "Hai ancora {minutes_left} min per sbloccare e risparmiare ~€{savings}",
        "en": "You still have {minutes_left} min to unlock and save ~€{savings}",
    },
    "unlock_reminder.mid.title": {
        "it": "Hai ancora {minutes_left} min",
        "en": "{minutes_left} min left",
    },
    "unlock_reminder.mid.body": {
        "it": "{partner_name} ha già sbloccato. Sblocca per condividere il taxi.",
        "en": "{partner_name} already unlocked. Unlock to share the taxi.",
    },
    "unlock_reminder.last.title": {
        "it": "⏰ Ultima chance!",
        "en": "⏰ Last chance!",
    },
    "unlock_reminder.last.body": {
        "it": "Il match con {partner_name} scade tra {minutes_left} min. Sblocca ora o perdi il match.",
        "en": "Your match with {partner_name} expires in {minutes_left} min. Unlock now or lose it.",
    },
    "unlock_reminder.email.subject": {
        "it": "⏰ Ultima chance per sbloccare il match",
        "en": "⏰ Last chance to unlock your match",
    },
    "unlock_reminder.email.body": {
        "it": "{partner_name} ti sta aspettando. Hai ancora {minutes_left} min: {match_url}",
        "en": "{partner_name} is waiting for you. You have {minutes_left} min left: {match_url}",
    },

    # unlock_expired (partial-unlock deadlock timed out)
    "unlock_expired.payer.title": {
        "it": "Nessun addebito",
        "en": "No charge",
    },
    "unlock_expired.payer.body": {
        "it": "Il tuo partner non ha risposto in tempo. €0 addebitati. Cerchiamo qualcun altro!",
        "en": "Your partner didn't respond in time. €0 charged. We'll find someone else!",
    },
    "unlock_expired.non_payer.title": {
        "it": "Match scaduto",
        "en": "Match expired",
    },
    "unlock_expired.non_payer.body": {
        "it": "Non hai sbloccato in tempo. Cercheremo un nuovo partner per te.",
        "en": "You didn't unlock in time. We'll look for a new partner for you.",
    },

    # match.dissolved
    "match_dissolved.title": {
        "it": "Match annullato",
        "en": "Match cancelled",
    },
    "match_dissolved.body": {
        "it": "Il tuo match è stato annullato. Cerchiamo un nuovo partner per te.",
        "en": "Your match was cancelled. We'll look for a new partner for you.",
    },

    # match.expired (flight departed, never completed)
    "match_expired.title": {
        "it": "Match scaduto",
        "en": "Match expired",
    },
    "match_expired.body": {
        "it": "Il volo è partito e il match non è stato completato.",
        "en": "The flight has departed and the match wasn't completed.",
    },

    # match.invalidated (flight delay cancelled a confirmed match)
    "match_invalidated.title": {
        "it": "Match annullato",
        "en": "Match cancelled",
    },
    "match_invalidated.body": {
        "it": "Il tuo match è stato annullato a causa di un ritardo del volo ({delta_min} min).",
        "en": "Your match was cancelled due to a flight delay ({delta_min} min).",
    },

    # trip.expired (no match found)
    "trip_expired.title": {
        "it": "Nessun partner trovato 😔",
        "en": "No partner found 😔",
    },
    "trip_expired.body": {
        "it": "Non siamo riusciti a trovare un partner per il tuo viaggio questa volta. Riprova al prossimo volo!",
        "en": "We couldn't find a match for your trip this time. Try again on your next flight!",
    },

    # trip.completed → review request
    "review_requested.title": {
        "it": "Com'è andata?",
        "en": "How was it?",
    },
    "review_requested.body": {
        "it": "Il tuo viaggio condiviso è completato. Lascia una recensione al tuo partner.",
        "en": "Your shared trip is complete. Leave a review for your partner.",
    },
}
