"""Load the fraud-rules guide into pgvector (`make ingest`).

1. Read every Markdown file in knowledge_base/.
2. Split each file by heading into chunks of about 300 words.
3. Turn each chunk into a 384-number vector ("embedding") with all-MiniLM-L6-v2. Texts with
   similar meaning get vectors that point in similar directions.
4. Store chunks + vectors in the guide_chunks table. Re-running replaces the old chunks.
"""

import asyncio
import re
from functools import lru_cache
from pathlib import Path

from sentence_transformers import SentenceTransformer
from sqlalchemy import delete

from app import db
from app.models import GuideChunk

KNOWLEDGE_BASE = Path("knowledge_base")
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
MAX_WORDS = 300


@lru_cache
def get_embedder() -> SentenceTransformer:
    """Load the embedding model once (it runs locally, no API key needed)."""
    return SentenceTransformer(EMBEDDING_MODEL)


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts as unit-length vectors (so cosine similarity is a plain dot product)."""
    vectors = get_embedder().encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vectors]


def split_markdown(text: str, max_words: int = MAX_WORDS) -> list[str]:
    """Split a Markdown document at headings, then cut long sections into ~max_words pieces.

    Each piece keeps its heading so it still makes sense on its own.
    """
    sections = [s.strip() for s in re.split(r"(?m)^(?=#{1,3} )", text) if s.strip()]
    chunks: list[str] = []
    for section in sections:
        heading, _, body = section.partition("\n")
        if not heading.startswith("#"):
            heading, body = "", section
        words = body.split()
        if not words:
            continue  # a heading with nothing under it (e.g. the document title)
        for i in range(0, len(words), max_words):
            piece = " ".join(words[i : i + max_words])
            chunks.append(f"{heading}\n{piece}".strip())
    return chunks


async def ingest(folder: Path = KNOWLEDGE_BASE) -> int:
    """Replace all guide chunks with fresh ones from `folder`. Returns the chunk count."""
    rows: list[tuple[str, int, str]] = []
    for path in sorted(folder.glob("*.md")):
        for i, chunk in enumerate(split_markdown(path.read_text(encoding="utf-8"))):
            rows.append((path.name, i, chunk))
    vectors = await asyncio.to_thread(embed, [content for _, _, content in rows])

    async with db.session_factory()() as session:
        await session.execute(delete(GuideChunk))
        session.add_all(
            GuideChunk(source=src, chunk_index=i, content=content, embedding=vec)
            for (src, i, content), vec in zip(rows, vectors, strict=True)
        )
        await session.commit()
    return len(rows)


async def main() -> None:
    """Command-line entry point."""
    await db.connect()
    try:
        count = await ingest()
    finally:
        await db.disconnect()
    print(f"Ingested {count} chunks from {KNOWLEDGE_BASE}/ into guide_chunks.")


if __name__ == "__main__":
    asyncio.run(main())
