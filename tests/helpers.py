"""Small helpers shared by tests."""

import random
import uuid
from datetime import datetime
from decimal import Decimal

from app import db
from app.models import Card, Payment
from app.security import generate_test_card_number, tokenize_card

_rng = random.Random(1234)


async def make_card(
    balance: str = "1000.00",
    status: str = "active",
    expiry: tuple[int, int] = (12, 2030),
    home: tuple[float, float] = (40.71, -74.0),
) -> tuple[str, int]:
    """Insert a card and return (card_number, card_id)."""
    number = generate_test_card_number(_rng)
    async with db.session_factory()() as s:
        card = Card(
            card_token=tokenize_card(number),
            last4=number[-4:],
            expiry_month=expiry[0],
            expiry_year=expiry[1],
            status=status,
            balance=Decimal(balance),
            home_lat=home[0],
            home_lon=home[1],
        )
        s.add(card)
        await s.commit()
        return number, card.id


async def add_history(card_id: int, rows: list[tuple[datetime, float, float, float]]) -> None:
    """Insert past payments (time, amount, lat, lon) for a card."""
    async with db.session_factory()() as s:
        for time, amount, lat, lon in rows:
            s.add(
                Payment(
                    card_id=card_id,
                    amount=Decimal(str(amount)),
                    merchant_name="Past shop",
                    merchant_category="grocery_pos",
                    merchant_lat=lat,
                    merchant_lon=lon,
                    status="APPROVED",
                    response_code="00",
                    source="seed",
                    created_at=time,
                )  # fmt: skip
            )
        await s.commit()


def payment_body(number: str, amount: float = 20.0, **overrides) -> dict:
    """A valid payment request body."""
    body = {
        "card_number": number,
        "expiry_month": 12,
        "expiry_year": 2030,
        "amount": amount,
        "merchant_name": "Store 42",
        "merchant_category": "grocery_pos",
        "merchant_lat": 40.71,
        "merchant_lon": -74.0,
    }
    body.update(overrides)
    return body


def headers(key: str | None = None) -> dict[str, str]:
    """API key + a fresh (or given) Idempotency-Key."""
    return {"X-API-Key": "test-api-key", "Idempotency-Key": key or str(uuid.uuid4())}
