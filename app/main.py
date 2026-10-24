"""FastAPI app: API routes and the dashboard."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Open database connections on startup and close them on shutdown."""
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(title="PayShield", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Report whether PostgreSQL and MongoDB are reachable."""
    status = await db.health()
    return {"status": "ok" if all(v == "ok" for v in status.values()) else "degraded", **status}
