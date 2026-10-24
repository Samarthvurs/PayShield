"""SQLAlchemy table definitions (PostgreSQL).

Money lives here because Postgres gives ACID transactions: a payment row and the matching
balance change are committed together or not at all.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 384  # size of all-MiniLM-L6-v2 vectors


class Base(DeclarativeBase):
    """Base class for all tables."""


class Card(Base):
    """A payment card. The full number is never stored: only its token and last 4 digits."""

    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_token: Mapped[str] = mapped_column(String(64), unique=True)
    last4: Mapped[str] = mapped_column(String(4))
    expiry_month: Mapped[int]
    expiry_year: Mapped[int]
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | blocked
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    home_lat: Mapped[float]
    home_lon: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Payment(Base):
    """One payment attempt, approved or declined."""

    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_card_id_created_at", "card_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Nullable: a payment with an unknown card number is still recorded (declined, code 14).
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    merchant_name: Mapped[str] = mapped_column(String(200))
    merchant_category: Mapped[str] = mapped_column(String(40))
    merchant_lat: Mapped[float]
    merchant_lon: Mapped[float]
    status: Mapped[str] = mapped_column(String(10))  # APPROVED | DECLINED
    response_code: Mapped[str] = mapped_column(String(2))
    risk_score: Mapped[int | None]
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    # Ground truth from the dataset, for seeded history only. NEVER read by the fraud checker.
    is_fraud_label: Mapped[bool | None]
    # "seed" = history loaded from the dataset, "api" = sent through POST /api/payments.
    source: Mapped[str] = mapped_column(String(10), default="api", index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class GuideChunk(Base):
    """A piece of the fraud-rules guide with its embedding, used for RAG search."""

    __tablename__ = "guide_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(100))
    chunk_index: Mapped[int]
    content: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
