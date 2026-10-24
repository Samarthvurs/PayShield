"""The payment flow: from a request to APPROVED / DECLINED.

Order of checks (each one can stop the flow with a decline code):
  idempotency -> Luhn (14) -> card lookup (14) -> blocked (05) -> expired (54)
  -> balance (51) -> fraud check (59) -> approve (00) and deduct the balance.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Card, Payment
from app.schemas import PaymentRequest, PaymentResponse
from app.security import luhn_valid, tokenize_card

RESPONSE_MESSAGES: dict[str, str] = {
    "00": "Approved",
    "05": "Card blocked",
    "14": "Invalid card",
    "51": "Insufficient funds",
    "54": "Expired card",
    "59": "Suspected fraud",
}


def response_from(payment: Payment) -> PaymentResponse:
    """Build the API response from a saved payment row."""
    return PaymentResponse(
        payment_id=payment.id,
        status=payment.status,
        response_code=payment.response_code,
        message=RESPONSE_MESSAGES[payment.response_code],
        risk_score=payment.risk_score,
    )


def is_expired(month: int, year: int, now: datetime) -> bool:
    """A card is valid until the end of its expiry month."""
    return (year, month) < (now.year, now.month)


async def process_payment(
    session: AsyncSession, request: PaymentRequest, idempotency_key: str
) -> PaymentResponse:
    """Run one payment through every check and save the result.

    The whole thing is one database transaction. The card row is locked with
    SELECT ... FOR UPDATE, so if two payments for the same card arrive at the same moment,
    the second waits until the first has committed and then sees the new balance.
    """
    # 1. Idempotency: a retry with the same key gets the saved answer, never a second charge.
    existing = await session.scalar(
        select(Payment).where(Payment.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return response_from(existing)

    now = datetime.now(UTC)
    card: Card | None = None
    risk_score: int | None = None

    if not luhn_valid(request.card_number):
        code = "14"
    else:
        token = tokenize_card(request.card_number)
        card = await session.scalar(select(Card).where(Card.card_token == token).with_for_update())
        if card is None or (card.expiry_month, card.expiry_year) != (
            request.expiry_month,
            request.expiry_year,
        ):
            code = "14"
        elif card.status == "blocked":
            code = "05"
        elif is_expired(card.expiry_month, card.expiry_year, now):
            code = "54"
        elif card.balance < request.amount:
            code = "51"
        else:
            risk_score = 0  # placeholder until the fraud checker is added
            code = "00"

    if code == "00" and card is not None:
        card.balance = card.balance - Decimal(request.amount)

    payment = Payment(
        card_id=card.id if card is not None else None,
        amount=request.amount,
        merchant_name=request.merchant_name,
        merchant_category=request.merchant_category,
        merchant_lat=request.merchant_lat,
        merchant_lon=request.merchant_lon,
        status="APPROVED" if code == "00" else "DECLINED",
        response_code=code,
        risk_score=risk_score,
        idempotency_key=idempotency_key,
        source="api",
        created_at=now,
    )
    session.add(payment)
    try:
        await session.commit()
    except IntegrityError:
        # Two requests with the same Idempotency-Key raced; the other one won. Return its answer.
        await session.rollback()
        winner = await session.scalar(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        )
        if winner is None:
            raise
        return response_from(winner)
    return response_from(payment)
