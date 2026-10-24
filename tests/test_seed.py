"""Phase 1: seeding fills the database, is safe to re-run, and never stores card numbers."""

import json
from pathlib import Path

from sqlalchemy import func, select, text

from app import db
from app.fraud.features import load_dataset
from app.models import Card, Payment
from app.security import luhn_valid
from scripts.seed import fake_dataset, pick_cardholders, seed

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transactions.csv"


async def _counts() -> tuple[int, int]:
    async with db.session_factory()() as s:
        cards = await s.scalar(select(func.count()).select_from(Card))
        payments = await s.scalar(select(func.count()).select_from(Payment))
    return cards, payments


async def test_seed_from_fixture_and_rerun_is_safe(database: None, tmp_path: Path) -> None:
    df = pick_cardholders(load_dataset(FIXTURE), n_cards=4)
    out = tmp_path / "test_cards.json"

    first = await seed(df, out)
    assert first["cards"] == 4 + 3  # 4 dataset cardholders + 3 special cards
    assert first["payments"] == len(df)
    assert await _counts() == (7, len(df))

    second = await seed(df, out)  # re-run: nothing new
    assert second["cards"] == 0 and second["payments"] == 0
    assert await _counts() == (7, len(df))

    cards = json.loads(out.read_text())
    assert len(cards) == 6
    assert all(luhn_valid(c["card_number"]) for c in cards)


async def test_no_full_card_number_is_stored(database: None, tmp_path: Path) -> None:
    df = pick_cardholders(load_dataset(FIXTURE), n_cards=3)
    out = tmp_path / "test_cards.json"
    await seed(df, out)
    numbers = [c["card_number"] for c in json.loads(out.read_text())]

    # Dump every text value from every table and search for any full card number.
    async with db.get_engine().connect() as conn:
        dump = ""
        for table in ("cards", "payments"):
            rows = (await conn.execute(text(f"SELECT * FROM {table}"))).all()
            dump += " ".join(str(v) for row in rows for v in row)
    for number in numbers:
        assert number not in dump
        assert number[-4:] in dump  # last 4 digits are kept, which is fine


async def test_faker_fallback_has_same_shape(database: None) -> None:
    df = fake_dataset(n_cards=2, per_card=10)
    assert set(load_dataset(FIXTURE).columns) <= set(df.columns)
    counts = await seed(df)
    assert counts["payments"] == 20
