"""Phase 2: the payment API."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app import db
from app.models import Card, Payment
from tests.helpers import headers, make_card, payment_body

URL = "/api/payments"


async def _balance(card_id: int) -> Decimal:
    async with db.session_factory()() as s:
        return (await s.get(Card, card_id)).balance


async def test_approved_payment_deducts_balance(client: httpx.AsyncClient) -> None:
    number, card_id = await make_card(balance="100.00")
    r = await client.post(URL, json=payment_body(number, 30.0), headers=headers())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "APPROVED" and body["response_code"] == "00"
    assert body["message"] == "Approved"
    assert await _balance(card_id) == Decimal("70.00")


async def test_invalid_luhn_is_code_14(client: httpx.AsyncClient) -> None:
    r = await client.post(URL, json=payment_body("4111111111111112"), headers=headers())
    assert r.json()["response_code"] == "14"
    assert r.json()["status"] == "DECLINED"


async def test_unknown_card_is_code_14(client: httpx.AsyncClient) -> None:
    r = await client.post(URL, json=payment_body("4111111111111111"), headers=headers())
    assert r.json()["response_code"] == "14"


async def test_wrong_expiry_is_code_14(client: httpx.AsyncClient) -> None:
    number, _ = await make_card()
    r = await client.post(URL, json=payment_body(number, expiry_year=2031), headers=headers())
    assert r.json()["response_code"] == "14"


async def test_blocked_card_is_code_05(client: httpx.AsyncClient) -> None:
    number, _ = await make_card(status="blocked")
    r = await client.post(URL, json=payment_body(number), headers=headers())
    assert r.json()["response_code"] == "05"


async def test_expired_card_is_code_54(client: httpx.AsyncClient) -> None:
    number, _ = await make_card(expiry=(1, 2024))
    body = payment_body(number, expiry_month=1, expiry_year=2024)
    r = await client.post(URL, json=body, headers=headers())
    assert r.json()["response_code"] == "54"


async def test_insufficient_funds_is_code_51(client: httpx.AsyncClient) -> None:
    number, card_id = await make_card(balance="10.00")
    r = await client.post(URL, json=payment_body(number, 10.01), headers=headers())
    assert r.json()["response_code"] == "51"
    assert await _balance(card_id) == Decimal("10.00")


async def test_idempotent_retry_charges_once(client: httpx.AsyncClient) -> None:
    number, card_id = await make_card(balance="100.00")
    h = headers("retry-key-1")
    first = await client.post(URL, json=payment_body(number, 40.0), headers=h)
    second = await client.post(URL, json=payment_body(number, 40.0), headers=h)
    assert first.json() == second.json()
    assert await _balance(card_id) == Decimal("60.00")
    async with db.session_factory()() as s:
        assert await s.scalar(select(func.count()).select_from(Payment)) == 1


async def test_simultaneous_retries_with_same_key_charge_once(client: httpx.AsyncClient) -> None:
    number, card_id = await make_card(balance="100.00")
    h = headers("race-key")
    results = await asyncio.gather(
        *[client.post(URL, json=payment_body(number, 40.0), headers=h) for _ in range(5)]
    )
    assert len({r.json()["payment_id"] for r in results}) == 1
    assert await _balance(card_id) == Decimal("60.00")


async def test_two_simultaneous_payments_cannot_overspend(client: httpx.AsyncClient) -> None:
    number, card_id = await make_card(balance="100.00")
    results = await asyncio.gather(
        client.post(URL, json=payment_body(number, 60.0), headers=headers()),
        client.post(URL, json=payment_body(number, 60.0), headers=headers()),
    )
    codes = sorted(r.json()["response_code"] for r in results)
    assert codes == ["00", "51"]  # exactly one succeeds
    assert await _balance(card_id) == Decimal("40.00")


async def test_wrong_api_key_is_401(client: httpx.AsyncClient) -> None:
    number, _ = await make_card()
    h = {**headers(), "X-API-Key": "wrong"}
    r = await client.post(URL, json=payment_body(number), headers=h)
    assert r.status_code == 401
    assert r.json() == {"error": "Invalid API key"}


async def test_missing_idempotency_key_is_422(client: httpx.AsyncClient) -> None:
    number, _ = await make_card()
    r = await client.post(URL, json=payment_body(number), headers={"X-API-Key": "test-api-key"})
    assert r.status_code == 422


async def test_validation_error_never_echoes_card_number(client: httpx.AsyncClient) -> None:
    number, _ = await make_card()
    r = await client.post(
        URL, json=payment_body(number, -5, merchant_category="x"), headers=headers()
    )
    assert r.status_code == 422
    assert number not in r.text
    fields = {d["field"] for d in r.json()["details"]}
    assert {"amount", "merchant_category"} <= fields


async def test_list_get_and_stats(client: httpx.AsyncClient) -> None:
    number, _ = await make_card(balance="50.00")
    ok = (await client.post(URL, json=payment_body(number, 20.0), headers=headers())).json()
    await client.post(URL, json=payment_body(number, 100.0), headers=headers())  # 51

    listing = (await client.get("/api/payments")).json()
    assert listing["total"] == 2
    assert listing["items"][0]["card_last4"] == number[-4:]
    declined = (await client.get("/api/payments", params={"status": "DECLINED"})).json()
    assert declined["total"] == 1 and declined["items"][0]["response_code"] == "51"

    one = await client.get(f"/api/payments/{ok['payment_id']}")
    assert one.status_code == 200 and one.json()["status"] == "APPROVED"
    missing = await client.get("/api/payments/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404 and missing.json() == {"error": "Payment not found"}

    stats = (await client.get("/api/stats")).json()
    assert stats["total_payments"] == 2
    assert stats["approval_rate"] == 0.5
    assert stats["fraud_decline_rate"] == 0.0
    assert sum(b["approved"] + b["declined"] for b in stats["over_time"]) == 2


async def test_empty_stats(client: httpx.AsyncClient) -> None:
    stats = (await client.get("/api/stats")).json()
    assert stats["total_payments"] == 0 and stats["approval_rate"] == 0.0


async def test_mongo_outage_does_not_fail_the_payment(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import payments

    def broken_mongo():
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(payments.db, "get_mongo", broken_mongo)
    number, card_id = await make_card(balance="100.00")
    r = await client.post(URL, json=payment_body(number, 10.0), headers=headers())
    assert r.status_code == 200 and r.json()["response_code"] == "00"
    assert await _balance(card_id) == Decimal("90.00")
