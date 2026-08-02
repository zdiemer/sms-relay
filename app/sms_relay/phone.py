"""Phone number normalization.

Every number crossing the API boundary is stored in E.164. Callers tend to
carry whatever their signup form accepted — bare 10-digit strings, dashes,
parens — each with its own ad-hoc regex. Normalizing centrally here is most of
the reason to have a shared service at all.
"""

from __future__ import annotations

import phonenumbers

from sms_relay.config import settings


class InvalidPhoneNumber(ValueError):
    pass


def normalize(raw: str, region: str | None = None) -> str:
    """Return `raw` as E.164, e.g. "+15555551234".

    Accepts anything libphonenumber accepts for the region — bare 10-digit US
    numbers, dashes, parens, a leading +1 — so existing callers need no changes.
    """
    if not raw or not raw.strip():
        raise InvalidPhoneNumber("phone number is empty")

    candidate = raw.strip()
    try:
        parsed = phonenumbers.parse(candidate, region or settings.default_region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumber(f"could not parse {candidate!r}: {exc}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumber(f"{candidate!r} is not a valid phone number")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def redact(number: str | None) -> str:
    """Mask all but the last 4 digits, for logs.

    This service holds every message for every consumer, which makes its logs a
    far juicier target than any single caller's. Never log a full number.
    """
    if not number:
        return "<none>"
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"
