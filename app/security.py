"""Card security helpers: Luhn check, tokenization and the API key check."""

import hashlib
import hmac
import random

from fastapi import Header, HTTPException, status

from app.config import get_settings


def luhn_valid(number: str) -> bool:
    """Return True if `number` passes the Luhn checksum used by all card networks.

    From the right, double every second digit (subtract 9 if it goes above 9) and add
    everything up. Valid numbers give a total divisible by 10. It catches typos such as one
    wrong digit or two swapped digits.
    """
    if not number.isdigit() or not 12 <= len(number) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        digit = int(ch)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def luhn_check_digit(partial: str) -> str:
    """Return the digit that makes `partial + digit` Luhn-valid."""
    for digit in "0123456789":
        if luhn_valid(partial + digit):
            return digit
    raise ValueError("unreachable")  # pragma: no cover - one digit always works


def generate_test_card_number(rng: random.Random, prefix: str = "4") -> str:
    """Make a random 16-digit, Luhn-valid test card number (Visa-style, starts with 4)."""
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(15 - len(prefix)))
    return body + luhn_check_digit(body)


SEED_EXPIRY = (12, 2030)  # expiry date given to every seeded test card


def test_card_number_for(cardholder_id: str | int) -> str:
    """The test card number given to a dataset cardholder.

    It's derived from the dataset's cardholder id, so the seed script and the demo traffic
    script agree on it without the number ever being saved anywhere.
    """
    return generate_test_card_number(random.Random(f"payshield-card-{cardholder_id}"))


def tokenize_card(card_number: str) -> str:
    """Turn a card number into a token with HMAC-SHA256 and a server secret.

    The same card always gives the same token, so we can look cards up, but the token can't
    be reversed into the number without the secret. Only the token and last 4 digits are
    ever stored.
    """
    secret = get_settings().card_token_secret.encode()
    return hmac.new(secret, card_number.encode(), hashlib.sha256).hexdigest()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject the request with 401 unless X-API-Key is correct."""
    expected = get_settings().api_key
    # compare_digest takes the same time whether the first or last character is wrong,
    # so an attacker can't guess the key one character at a time from response times.
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
