"""Find the guide chunks most relevant to a declined payment (the "R" in RAG)."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from app import db
from app.ai.ingest import embed
from app.models import GuideChunk


@dataclass(frozen=True)
class RetrievedChunk:
    """One guide chunk and how similar it is to the query (1.0 = same direction)."""

    source: str
    chunk_index: int
    content: str
    similarity: float

    @property
    def ref(self) -> str:
        """Citation id used in explanations, e.g. "fraud_rules_guide.md#2"."""
        return f"{self.source}#{self.chunk_index}"


def build_query(
    response_code: str, message: str, rules: Sequence[str], reasons: Sequence[str]
) -> str:
    """Turn what happened to a payment into a search query for the guide."""
    parts = [f"Payment declined with response code {response_code} ({message})."]
    if rules:
        parts.append("Fraud rules triggered: " + ", ".join(rules) + ".")
    parts.extend(reasons)
    return " ".join(parts)


async def retrieve(query: str, k: int = 3) -> list[RetrievedChunk]:
    """Top-k chunks by cosine similarity, using pgvector's `<=>` (cosine distance) operator."""
    [vector] = await asyncio.to_thread(embed, [query])
    distance = GuideChunk.embedding.cosine_distance(vector)
    async with db.session_factory()() as session:
        rows = (
            await session.execute(
                select(GuideChunk, distance.label("d")).order_by(distance).limit(k)
            )
        ).all()
    return [RetrievedChunk(c.source, c.chunk_index, c.content, 1 - float(d)) for c, d in rows]
