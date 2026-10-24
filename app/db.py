"""Connections to PostgreSQL (SQLAlchemy, async) and MongoDB (Motor, async).

Both clients are created once when the app starts (`connect`) and closed when it stops
(`disconnect`). Request handlers get a Postgres session from `get_session` and the Mongo
database from `get_mongo`.
"""

from collections.abc import AsyncIterator

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings


class _State:
    engine: AsyncEngine | None = None
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    mongo_client: AsyncIOMotorClient | None = None
    mongo_db: AsyncIOMotorDatabase | None = None


_state = _State()


async def connect(settings: Settings | None = None) -> None:
    """Open both database connections and make sure tables and indexes exist."""
    settings = settings or get_settings()
    _state.engine = create_async_engine(settings.database_url, pool_size=20, max_overflow=10)
    _state.sessionmaker = async_sessionmaker(_state.engine, expire_on_commit=False)
    _state.mongo_client = AsyncIOMotorClient(settings.mongo_url, serverSelectionTimeoutMS=3000)
    _state.mongo_db = _state.mongo_client[settings.mongo_db]
    await create_schema()


async def create_schema() -> None:
    """Create the pgvector extension, all tables and the Mongo indexes (safe to re-run)."""
    from app import models  # imported here so models can import from this module

    assert _state.engine is not None and _state.mongo_db is not None
    async with _state.engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(models.Base.metadata.create_all)
    await _state.mongo_db.fraud_checks.create_index("payment_id")
    await _state.mongo_db.ai_explanations.create_index("payment_id", unique=True)


async def disconnect() -> None:
    """Close both connections."""
    if _state.engine is not None:
        await _state.engine.dispose()
    if _state.mongo_client is not None:
        _state.mongo_client.close()
    _state.engine = _state.sessionmaker = _state.mongo_client = _state.mongo_db = None


def get_engine() -> AsyncEngine:
    """Return the open SQLAlchemy engine."""
    assert _state.engine is not None, "call db.connect() first"
    return _state.engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory (used by scripts and background work)."""
    assert _state.sessionmaker is not None, "call db.connect() first"
    return _state.sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one Postgres session per request."""
    async with session_factory()() as session:
        yield session


def get_mongo() -> AsyncIOMotorDatabase:
    """Return the Mongo database."""
    assert _state.mongo_db is not None, "call db.connect() first"
    return _state.mongo_db


async def health() -> dict[str, str]:
    """Ping both databases. Returns {"postgres": "ok"|"error", "mongo": "ok"|"error"}."""
    result = {"postgres": "error", "mongo": "error"}
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        result["postgres"] = "ok"
    except Exception:  # noqa: BLE001 - health must never raise
        pass
    try:
        await get_mongo().command("ping")
        result["mongo"] = "ok"
    except Exception:  # noqa: BLE001
        pass
    return result
