"""Check that retrieval finds the right guide file (`make evaluate`).

10 test questions, each with the guide file that should answer it. "Hit rate @3" is the
share of questions where that file is among the top 3 retrieved chunks. The result is
written to the RAG section of docs/evaluation.md.
"""

import argparse
import asyncio
from pathlib import Path

from app import db
from app.ai.ingest import ingest
from app.ai.retriever import retrieve
from scripts.evaluate import write_section

START, END = "<!-- RAG_EVAL_START -->", "<!-- RAG_EVAL_END -->"

QUESTIONS: list[tuple[str, str]] = [
    (
        "Six payments on this card within 60 seconds. Why is that suspicious?",
        "fraud_rules_guide.md",
    ),
    (
        "The amount is many standard deviations above the card's usual spending.",
        "fraud_rules_guide.md",
    ),
    ("The payment was 2,000 km from the previous one ten minutes earlier.", "fraud_rules_guide.md"),
    ("What does the machine learning fraud score mean?", "fraud_rules_guide.md"),
    (
        "What does response code 51 insufficient funds mean and what should I do?",
        "decline_codes.md",
    ),
    ("My card was declined with code 05, card blocked.", "decline_codes.md"),
    ("Payment declined with code 54 because the card expired.", "decline_codes.md"),
    ("Criminals check stolen card numbers with many tiny online payments.", "fraud_patterns.md"),
    ("Why is a lot of fraud committed late at night online?", "fraud_patterns.md"),
    ("A genuine customer was wrongly declined. What is a false positive?", "fraud_patterns.md"),
]


async def hit_rate(k: int = 3) -> tuple[float, list[tuple[str, str, list[str], bool]]]:
    """Run every question; return (hit rate, per-question details)."""
    details = []
    for question, expected in QUESTIONS:
        sources = [c.source for c in await retrieve(question, k=k)]
        details.append((question, expected, sources, expected in sources))
    return sum(d[3] for d in details) / len(details), details


def render(rate: float, details: list[tuple[str, str, list[str], bool]]) -> str:
    """The RAG section of docs/evaluation.md."""
    hits = sum(d[3] for d in details)
    lines = [
        "## RAG retrieval check",
        "",
        f"**Hit rate @3: {rate:.0%} ({hits}/{len(details)} questions)** — the expected guide "
        "file was among the top 3 chunks returned by pgvector.",
        "",
        "| Question | Expected file | Top-3 files | Hit |",
        "|---|---|---|---|",
    ]
    for question, expected, sources, hit in details:
        lines.append(
            f"| {question} | `{expected}` | {', '.join(sources)} | {'✅' if hit else '❌'} |"
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    """Command-line entry point. Ingests the guide first so the check is up to date."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc", default="docs/evaluation.md")
    args = parser.parse_args()
    await db.connect()
    try:
        await ingest()
        rate, details = await hit_rate()
    finally:
        await db.disconnect()
    write_section(Path(args.doc), START, END, render(rate, details))
    print(f"RAG hit rate @3: {rate:.0%}. Wrote {args.doc}")


if __name__ == "__main__":
    asyncio.run(main())
