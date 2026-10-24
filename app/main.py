"""FastAPI app: API routes and the dashboard."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.models import Card, Payment
from app.payments import RESPONSE_MESSAGES, process_payment
from app.schemas import (
    FraudDetails,
    PaymentList,
    PaymentOut,
    PaymentRequest,
    PaymentResponse,
    Stats,
    TimeBucket,
)
from app.security import require_api_key

logger = logging.getLogger("payshield")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Open database connections on startup and close them on shutdown."""
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(title="PayShield", version="1.0.0", lifespan=lifespan)
Session = Annotated[AsyncSession, Depends(db.get_session)]


# ---------- Clean JSON errors (no stack traces, never echo the card number) ----------


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """422 with field names and messages only. FastAPI's default would echo the input,
    which could include a full card number."""
    details = [
        {"field": ".".join(str(p) for p in err["loc"] if p != "body"), "message": err["msg"]}
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"error": "Invalid request", "details": details})


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Errors raised on purpose (401, 404...) as {"error": "..."}."""
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(Exception)
async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    """Anything unexpected: log it on the server, return a plain message to the client."""
    logger.exception("Unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


# ---------- Health ----------


@app.get("/health")
async def health() -> dict[str, str]:
    """Report whether PostgreSQL and MongoDB are reachable."""
    status = await db.health()
    return {"status": "ok" if all(v == "ok" for v in status.values()) else "degraded", **status}


# ---------- Payments ----------


@app.post("/api/payments", response_model=PaymentResponse, dependencies=[Depends(require_api_key)])
async def create_payment(
    body: PaymentRequest,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=100)],
) -> PaymentResponse:
    """Authorize a payment. Send a unique Idempotency-Key per payment; retries reuse it."""
    return await process_payment(session, body, idempotency_key)


async def _fraud_checks(payment_ids: list[UUID]) -> dict[str, dict]:
    """Fetch fraud-check logs from MongoDB for the given payments, keyed by payment id."""
    if not payment_ids:
        return {}
    cursor = db.get_mongo().fraud_checks.find(
        {"payment_id": {"$in": [str(p) for p in payment_ids]}}
    )
    return {doc["payment_id"]: doc async for doc in cursor}


def _payment_out(
    payment: Payment, last4: str | None, check: dict | None, detail: bool
) -> PaymentOut:
    return PaymentOut(
        payment_id=payment.id,
        card_last4=last4,
        amount=float(payment.amount),
        merchant_name=payment.merchant_name,
        merchant_category=payment.merchant_category,
        merchant_lat=payment.merchant_lat,
        merchant_lon=payment.merchant_lon,
        status=payment.status,
        response_code=payment.response_code,
        message=RESPONSE_MESSAGES[payment.response_code],
        risk_score=payment.risk_score,
        rules_triggered=check["rules_triggered"] if check else [],
        created_at=payment.created_at,
        fraud=FraudDetails(**check) if check and detail else None,
    )


@app.get("/api/payments", response_model=PaymentList)
async def list_payments(
    session: Session,
    status: Literal["APPROVED", "DECLINED"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PaymentList:
    """Newest payments first (API payments only, not the seeded history)."""
    filters = [Payment.source == "api"]
    if status:
        filters.append(Payment.status == status)
    total = await session.scalar(select(func.count()).select_from(Payment).where(*filters)) or 0
    rows = (
        await session.execute(
            select(Payment, Card.last4)
            .outerjoin(Card, Payment.card_id == Card.id)
            .where(*filters)
            .order_by(Payment.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    checks = await _fraud_checks([p.id for p, _ in rows])
    items = [_payment_out(p, last4, checks.get(str(p.id)), detail=False) for p, last4 in rows]
    return PaymentList(items=items, total=total, page=page, page_size=page_size)


@app.get("/api/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: UUID, session: Session) -> PaymentOut:
    """One payment, with its fraud-check details if it had one."""
    row = (
        await session.execute(
            select(Payment, Card.last4)
            .outerjoin(Card, Payment.card_id == Card.id)
            .where(Payment.id == payment_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    payment, last4 = row
    check = (await _fraud_checks([payment.id])).get(str(payment.id))
    return _payment_out(payment, last4, check, detail=True)


@app.get("/api/stats", response_model=Stats)
async def stats(session: Session) -> Stats:
    """Totals, approval rate, fraud-decline rate and payments over time."""
    api = Payment.source == "api"
    total, approved, fraud, avg_risk, first, last = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Payment.status == "APPROVED"),
                func.count().filter(Payment.response_code == "59"),
                func.avg(Payment.risk_score),
                func.min(Payment.created_at),
                func.max(Payment.created_at),
            ).where(api)
        )
    ).one()

    # Pick a bucket size that gives a readable chart for the time span we have.
    span = (last - first).total_seconds() if first and last else 0
    bucket_size = "minute" if span <= 3 * 3600 else "hour" if span <= 7 * 86400 else "day"
    bucket = func.date_trunc(bucket_size, Payment.created_at).label("bucket")
    rows = (
        await session.execute(
            select(
                bucket,
                func.sum(case((Payment.status == "APPROVED", 1), else_=0)),
                func.sum(case((Payment.status == "DECLINED", 1), else_=0)),
            )
            .where(api)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()
    return Stats(
        total_payments=total,
        approved=approved,
        declined=total - approved,
        fraud_declined=fraud,
        approval_rate=approved / total if total else 0.0,
        fraud_decline_rate=fraud / total if total else 0.0,
        avg_risk_score=round(float(avg_risk), 1) if avg_risk is not None else None,
        bucket_size=bucket_size,
        over_time=[TimeBucket(bucket=b, approved=a, declined=d) for b, a, d in rows],
    )
