"""Phase 5: RAG ingest, retrieval and explanations."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app import db
from app.ai import explainer
from app.ai.explainer import build_prompt, parse_explanation, safe_facts
from app.ai.ingest import ingest, split_markdown
from app.ai.retriever import RetrievedChunk, retrieve
from app.config import get_settings
from app.fraud.features import now_utc
from scripts.evaluate_rag import hit_rate
from tests.helpers import add_history, headers, make_card, payment_body


def test_split_markdown_keeps_headings_and_limits_size() -> None:
    text = "# Title\n\n## A\n" + "word " * 650 + "\n## B\nshort section"
    chunks = split_markdown(text, max_words=300)
    assert [c.split("\n")[0] for c in chunks] == ["## A", "## A", "## A", "## B"]
    assert all(len(c.split()) <= 302 for c in chunks)  # 300 words + the heading


async def test_ingest_replaces_chunks_and_retrieval_finds_right_file(database: None) -> None:
    first = await ingest()
    second = await ingest()  # re-running replaces, doesn't duplicate
    assert first == second > 10
    top = await retrieve("many small payments within one minute, card testing", k=3)
    assert len(top) == 3 and top[0].similarity >= top[2].similarity
    assert any(c.source in {"fraud_rules_guide.md", "fraud_patterns.md"} for c in top)
    rate, _ = await hit_rate()
    assert rate >= 0.8


def test_safe_facts_drop_everything_not_whitelisted() -> None:
    facts = safe_facts({"card_last4": "1234", "card_number": "4111111111111111", "amount": 5,
                        "card_token": "abc", "rules_triggered": ["too_fast"]})  # fmt: skip
    assert facts == {"card_last4": "1234", "amount": 5, "rules_triggered": ["too_fast"]}


def test_parse_explanation_drops_made_up_sources() -> None:
    text = 'Sure! {"summary": "s", "reasons": ["r"], "sources": ["a.md#0", "invented.md#9"]}'
    assert parse_explanation(text, {"a.md#0"}).sources == ["a.md#0"]
    with pytest.raises(ValueError):
        parse_explanation("no json here", set())


async def _declined_fraud_payment(client: httpx.AsyncClient) -> tuple[str, str]:
    number, card_id = await make_card(balance="1000.00")
    now = now_utc()
    await add_history(
        card_id, [(now - timedelta(seconds=50 - 10 * i), 2.0, 40.71, -74.0) for i in range(5)]
    )
    body = payment_body(number, 3.0, merchant_lat=34.05, merchant_lon=-118.24)
    r = await client.post("/api/payments", json=body, headers=headers())
    assert r.json()["response_code"] == "59"
    return number, r.json()["payment_id"]


async def test_explain_uses_fallback_without_api_key_and_caches(client: httpx.AsyncClient) -> None:
    await ingest()
    _, payment_id = await _declined_fraud_payment(client)
    first = (await client.get(f"/api/payments/{payment_id}/explain")).json()
    assert first["model"] == explainer.FALLBACK_MODEL and first["cached"] is False
    assert "Suspected fraud" in first["summary"]
    assert any("60 seconds" in r for r in first["reasons"])
    assert len(first["sources"]) == 3

    second = (await client.get(f"/api/payments/{payment_id}/explain")).json()
    assert second["cached"] is True and second["summary"] == first["summary"]
    assert await db.get_mongo().ai_explanations.count_documents({}) == 1


async def test_explain_approved_payment_is_400(client: httpx.AsyncClient) -> None:
    number, _ = await make_card()
    r = await client.post("/api/payments", json=payment_body(number), headers=headers())
    resp = await client.get(f"/api/payments/{r.json()['payment_id']}/explain")
    assert resp.status_code == 400


class FakeMessages:
    """Records prompts and returns canned replies, instead of calling Claude."""

    def __init__(self, replies: list[str]) -> None:
        self.replies, self.calls = replies, []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        text = self.replies.pop(0)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)]
        )


async def test_claude_path_never_sends_card_number_and_retries_bad_json(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ingest()
    number, payment_id = await _declined_fraud_payment(client)
    top = await retrieve("too_fast impossible_travel", k=3)
    good = f'{{"summary": "Declined as likely fraud.", "reasons": ["fast"], "sources": ["{top[0].ref}"]}}'
    fake = FakeMessages(["not json at all", good])
    monkeypatch.setattr(
        explainer.anthropic, "AsyncAnthropic", lambda **_: SimpleNamespace(messages=fake)
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    result = (await client.get(f"/api/payments/{payment_id}/explain")).json()
    get_settings.cache_clear()
    assert result["model"] == get_settings().llm_model
    assert result["summary"] == "Declined as likely fraud."
    assert len(fake.calls) == 2  # retried once after invalid JSON
    sent = str(fake.calls)
    assert number not in sent  # the full card number never reaches the LLM
    assert number[-4:] in sent


def test_prompt_contains_only_safe_facts() -> None:
    chunk = RetrievedChunk("fraud_rules_guide.md", 1, "## Rule\ntext", 0.9)
    prompt = build_prompt(
        safe_facts({"card_last4": "4242", "card_number": "4000000000004242"}), [chunk]
    )
    assert "4000000000004242" not in prompt and "fraud_rules_guide.md#1" in prompt
