"""SQLAlchemy table definitions (PostgreSQL)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all tables."""
