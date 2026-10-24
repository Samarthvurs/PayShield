"""Shared test setup.

Tests use separate databases (`payshield_test` in Postgres and Mongo) so they never touch
real data. Settings are overridden with environment variables *before* the app is imported.
"""

import os

_base = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://payshield:payshield@localhost:5433/payshield"
)
os.environ["DATABASE_URL"] = _base.rsplit("/", 1)[0] + "/payshield_test"
os.environ["MONGO_DB"] = "payshield_test"
os.environ["API_KEY"] = "test-api-key"
os.environ["CARD_TOKEN_SECRET"] = "test-secret"
os.environ["GEMINI_API_KEY"] = ""
os.environ["APP_ENV"] = "dev"
os.environ["MODEL_PATH"] = "models/__no_model_in_tests__.joblib"

from collections.abc import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402

API_HEADERS = {"X-API-Key": "test-api-key"}


def _ensure_test_database() -> None:
    """Create the payshield_test database if it doesn't exist yet."""
    url = os.environ["DATABASE_URL"].replace("+psycopg", "")
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'payshield_test'"
        ).fetchone()
        if not exists:
            conn.execute("CREATE DATABASE payshield_test")


_ensure_test_database()


@pytest.fixture
async def database() -> AsyncIterator[None]:
    """Connect to empty test databases for one test."""
    get_settings.cache_clear()
    await db.connect()
    async with db.get_engine().begin() as conn:
        tables = ", ".join(t.name for t in reversed(_tables()))
        if tables:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    mongo = db.get_mongo()
    for name in await mongo.list_collection_names():
        await mongo[name].delete_many({})
    yield
    await db.disconnect()


def _tables() -> list:
    from app.models import Base

    return list(Base.metadata.sorted_tables)


@pytest.fixture
async def client(database: None) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client that calls the app in-process."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
