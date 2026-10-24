"""Explain a declined payment in plain English (RAG = retrieval-augmented generation).

1. Collect SAFE facts about the payment: last 4 digits, amount, category, scores, rules.
   The full card number is never available here (it's never stored) and never sent.
2. Retrieve the 3 most relevant chunks of the fraud-rules guide (pgvector search).
3. Ask Claude to explain the decline using ONLY those chunks, as JSON.
4. Validate the JSON with Pydantic; retry once if it's invalid.
5. No API key, an API error, or a refusal -> a deterministic fallback built from the
   triggered rules and the same chunks.
6. Cache the result in MongoDB so each payment is explained once.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import anthropic
from pydantic import ValidationError

from app import db
from app.ai.retriever import RetrievedChunk, build_query, retrieve
from app.config import get_settings
from app.schemas import Explanation, ExplanationOut

logger = logging.getLogger("payshield.ai")

FALLBACK_MODEL = "fallback-template"

SYSTEM_PROMPT = """You explain why a card payment was declined, for the cardholder and a support agent.

Rules:
- Use ONLY the facts about the payment and the guide excerpts you are given.
- Never invent rules, thresholds, or facts that are not in them.
- Write plain, friendly English. No jargon without a short explanation.
- Reply with ONLY a JSON object, no other text, in exactly this shape:
  {"summary": "<one or two sentences>", "reasons": ["<short reason>", ...], "sources": ["<excerpt id>", ...]}
- "sources" must list the ids of the excerpts you used, e.g. "fraud_rules_guide.md#2"."""

# The only payment fields that may be sent to the LLM.
SAFE_FIELDS = (
    "card_last4", "amount", "merchant_category", "response_code", "message",
    "rule_score", "ml_score", "final_score", "threshold", "rules_triggered", "reasons",
)  # fmt: skip


def safe_facts(payment: dict[str, Any]) -> dict[str, Any]:
    """Keep only the whitelisted fields. Anything else (and any card number) is dropped."""
    return {k: payment[k] for k in SAFE_FIELDS if k in payment}


def build_prompt(facts: dict[str, Any], chunks: list[RetrievedChunk]) -> str:
    """The user message: payment facts + guide excerpts."""
    excerpts = "\n\n".join(f'<excerpt id="{c.ref}">\n{c.content}\n</excerpt>' for c in chunks)
    return (
        f"Payment facts (JSON):\n{json.dumps(facts, indent=2, default=str)}\n\n"
        f"Guide excerpts:\n{excerpts}\n\n"
        "Explain why this payment was declined."
    )


def parse_explanation(text: str, allowed_sources: set[str]) -> Explanation:
    """Parse and validate Claude's JSON. Unknown source ids are dropped (no made-up citations)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in the reply")
    explanation = Explanation.model_validate_json(text[start : end + 1])
    explanation.sources = [s for s in explanation.sources if s in allowed_sources]
    return explanation


def fallback_explanation(facts: dict[str, Any], chunks: list[RetrievedChunk]) -> Explanation:
    """A template explanation used when Claude isn't available."""
    code, message = facts.get("response_code", ""), facts.get("message", "Declined")
    summary = f"This payment of ${float(facts.get('amount', 0)):,.2f} was declined: {message} (code {code})."
    reasons = list(facts.get("reasons") or [])
    if code == "59" and facts.get("final_score") is not None:
        summary += (
            f" Its risk score was {facts['final_score']:g}, at or above the decline "
            f"threshold of {facts.get('threshold', 60):g}."
        )
        if facts.get("ml_score"):
            reasons.append(
                f"The machine-learning model gave a fraud score of {facts['ml_score']:g}/100."
            )
    if not reasons:
        reasons.append(f"Decline code {code} means: {message}.")
    if chunks:
        first_sentence = chunks[0].content.split("\n", 1)[-1].split(". ")[0].strip()
        reasons.append(f"From the guide: {first_sentence.rstrip('.')}.")
    return Explanation(summary=summary, reasons=reasons, sources=[c.ref for c in chunks])


async def ask_claude(prompt: str, allowed_sources: set[str]) -> Explanation | None:
    """Call Claude; retry once on invalid JSON. Returns None if it fails or refuses."""
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=30.0)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        response = await client.messages.create(
            model=settings.llm_model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=messages,
            extra_body={"output_config": {"effort": "low"}},  # short, simple task
        )
        if response.stop_reason == "refusal":
            logger.warning("Claude declined to explain; using the fallback.")
            return None
        text = "".join(
            getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
        )
        try:
            return parse_explanation(text, allowed_sources)
        except (ValueError, ValidationError) as exc:
            logger.warning("Invalid JSON from Claude (attempt %d): %s", attempt + 1, exc)
            messages += [
                {"role": "assistant", "content": text or "(empty)"},
                {
                    "role": "user",
                    "content": "That was not valid JSON in the required shape. "
                    "Reply with ONLY the JSON object.",
                },  # fmt: skip
            ]
    return None


async def explain(payment: dict[str, Any]) -> ExplanationOut:
    """Explain one declined payment, using the Mongo cache when possible.

    `payment` holds the payment's details plus its fraud check (scores, rules, reasons).
    """
    collection = db.get_mongo().ai_explanations
    cached = await collection.find_one({"payment_id": str(payment["payment_id"])})
    if cached:
        return ExplanationOut(**cached, cached=True)

    facts = safe_facts(payment)
    query = build_query(
        facts.get("response_code", ""), facts.get("message", ""),
        facts.get("rules_triggered", []), facts.get("reasons", []),
    )  # fmt: skip
    chunks = await retrieve(query, k=3)

    result: Explanation | None = None
    model = FALLBACK_MODEL
    if get_settings().anthropic_api_key:
        try:
            result = await ask_claude(build_prompt(facts, chunks), {c.ref for c in chunks})
            model = get_settings().llm_model
        except anthropic.APIError as exc:
            logger.warning("Claude API error, using the fallback: %s", exc)
    if result is None:
        result, model = fallback_explanation(facts, chunks), FALLBACK_MODEL

    out = ExplanationOut(payment_id=payment["payment_id"], model=model, **result.model_dump())
    doc = {**out.model_dump(exclude={"cached"}), "payment_id": str(out.payment_id),
           "created_at": datetime.now(UTC)}  # fmt: skip
    await collection.update_one(
        {"payment_id": doc["payment_id"]}, {"$setOnInsert": doc}, upsert=True
    )
    return out
