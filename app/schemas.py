"""Pydantic models: the shapes of API requests and responses."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.fraud.features import CATEGORIES


class PaymentRequest(BaseModel):
    """Body of POST /api/payments."""

    # repr=False keeps the number out of logs and error messages if the object is printed.
    card_number: str = Field(pattern=r"^\d{12,19}$", repr=False, examples=["4111111111111111"])
    expiry_month: int = Field(ge=1, le=12)
    expiry_year: int = Field(ge=2000, le=2100)
    amount: Decimal = Field(gt=0, le=1_000_000, decimal_places=2, examples=[2499.00])
    merchant_name: str = Field(min_length=1, max_length=200)
    merchant_category: str = Field(examples=["grocery_pos"])
    merchant_lat: float = Field(ge=-90, le=90)
    merchant_lon: float = Field(ge=-180, le=180)

    @field_validator("merchant_category")
    @classmethod
    def known_category(cls, value: str) -> str:
        """Only the 14 dataset categories are allowed (the model was trained on them)."""
        if value not in CATEGORIES:
            raise ValueError(f"must be one of: {', '.join(CATEGORIES)}")
        return value


class PaymentResponse(BaseModel):
    """What POST /api/payments returns."""

    payment_id: UUID
    status: str
    response_code: str
    message: str
    risk_score: int | None


class FraudDetails(BaseModel):
    """Fraud-check details for one payment (from MongoDB)."""

    rule_score: float
    ml_score: float
    final_score: float
    threshold: float | None = None
    rules_triggered: list[str]
    reasons: list[str]
    features: dict[str, float | None]


class PaymentOut(BaseModel):
    """A payment as shown in the dashboard and GET endpoints."""

    payment_id: UUID
    card_last4: str | None
    amount: float
    merchant_name: str
    merchant_category: str
    merchant_lat: float
    merchant_lon: float
    status: str
    response_code: str
    message: str
    risk_score: int | None
    rules_triggered: list[str] = []
    created_at: datetime
    fraud: FraudDetails | None = None


class PaymentList(BaseModel):
    """One page of payments."""

    items: list[PaymentOut]
    total: int
    page: int
    page_size: int


class TimeBucket(BaseModel):
    """Approved and declined counts in one time bucket."""

    bucket: datetime
    approved: int
    declined: int


class Stats(BaseModel):
    """Dashboard numbers (payments sent through the API only, not seeded history)."""

    total_payments: int
    approved: int
    declined: int
    fraud_declined: int
    approval_rate: float
    fraud_decline_rate: float
    avg_risk_score: float | None
    bucket_size: str
    over_time: list[TimeBucket]


class Explanation(BaseModel):
    """AI explanation of a declined payment."""

    summary: str
    reasons: list[str]
    sources: list[str]


class ExplanationOut(Explanation):
    """Explanation plus where it came from."""

    payment_id: UUID
    model: str
    cached: bool = False
