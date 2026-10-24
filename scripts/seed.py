"""Fill the databases with cards and payment history (`make seed`).

From fraudTrain.csv we take N cardholders (default 300) and load all their past
transactions as payment history, so each card has a real spending pattern for the fraud
checks. Each cardholder gets a new Luhn-valid test card number; only its token and last 4
digits are stored.

Re-running is safe: cards that already exist (same token) are skipped with their history.
If the dataset isn't downloaded, Faker generates data of the same shape instead.
"""

import argparse
import asyncio
import json
import random
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
from faker import Faker
from sqlalchemy import insert, select

from app import db
from app.config import get_settings
from app.fraud.features import CATEGORIES, load_dataset
from app.models import Card, Payment
from app.security import (
    SEED_EXPIRY,
    generate_test_card_number,
    test_card_number_for,
    tokenize_card,
)

SEED_BALANCE = Decimal("50000.00")
EXPIRY = SEED_EXPIRY
BATCH = 5000


def pick_cardholders(df: pd.DataFrame, n_cards: int, seed: int = 42) -> pd.DataFrame:
    """Rows of `n_cards` randomly chosen cardholders (same choice on every run)."""
    ids = sorted(df["card_id"].unique())
    chosen = random.Random(seed).sample(ids, min(n_cards, len(ids)))
    return df[df["card_id"].isin(chosen)]


def fake_dataset(n_cards: int, per_card: int = 60, seed: int = 42) -> pd.DataFrame:
    """Synthetic transactions with the same columns as `load_dataset` returns."""
    fake = Faker()
    Faker.seed(seed)
    rng = random.Random(seed)
    start = pd.Timestamp("2019-01-01")
    rows = []
    for card in range(n_cards):
        card_id = 4_000_000_000_000 + card
        home_lat, home_lon = float(fake.latitude()), float(fake.longitude())
        time = start
        for _ in range(per_card):
            time += timedelta(hours=rng.expovariate(1 / 36))
            rows.append(
                {
                    "time": time,
                    "card_id": card_id,
                    "merchant_name": fake.company(),
                    "merchant_category": rng.choice(CATEGORIES),
                    "amount": round(rng.lognormvariate(3.5, 1.0), 2),
                    "home_lat": home_lat,
                    "home_lon": home_lon,
                    "merchant_lat": home_lat + rng.uniform(-1, 1),
                    "merchant_lon": home_lon + rng.uniform(-1, 1),
                    "is_fraud": int(rng.random() < 0.01),
                }
            )
    return pd.DataFrame(rows)


async def seed(df: pd.DataFrame, test_cards_path: Path | None = None) -> dict[str, int]:
    """Insert cards + history for every cardholder in `df`, plus 3 special test cards.

    Returns counts of what was inserted.
    """
    factory = db.session_factory()
    counts = {"cards": 0, "payments": 0, "skipped_cards": 0}
    test_cards: list[dict] = []

    async with factory() as session:
        existing = set((await session.scalars(select(Card.card_token))).all())

        for card_id, rows in df.sort_values("time").groupby("card_id", sort=True):
            number = test_card_number_for(card_id)
            if len(test_cards) < 3:
                test_cards.append(_test_card(f"Normal card {len(test_cards) + 1}", number))
            token = tokenize_card(number)
            if token in existing:
                counts["skipped_cards"] += 1
                continue
            first = rows.iloc[0]
            card = Card(
                card_token=token,
                last4=number[-4:],
                expiry_month=EXPIRY[0],
                expiry_year=EXPIRY[1],
                status="active",
                balance=SEED_BALANCE,
                home_lat=float(first["home_lat"]),
                home_lon=float(first["home_lon"]),
            )
            session.add(card)
            await session.flush()  # gets card.id
            payments = [
                {
                    "card_id": card.id,
                    "amount": Decimal(str(round(r.amount, 2))),
                    "merchant_name": r.merchant_name[:200],
                    "merchant_category": r.merchant_category,
                    "merchant_lat": float(r.merchant_lat),
                    "merchant_lon": float(r.merchant_lon),
                    "status": "APPROVED",
                    "response_code": "00",
                    "risk_score": None,
                    "idempotency_key": None,
                    "is_fraud_label": bool(r.is_fraud),
                    "source": "seed",
                    "created_at": r.time.to_pydatetime(),
                }
                for r in rows.itertuples(index=False)
            ]
            for i in range(0, len(payments), BATCH):
                await session.execute(insert(Payment), payments[i : i + BATCH])
            counts["cards"] += 1
            counts["payments"] += len(payments)

        # Three special cards to try each decline code from the dashboard.
        rng = random.Random("payshield-special-cards")
        specials = [
            ("Blocked card (code 05)", "blocked", SEED_BALANCE, EXPIRY),
            ("Expired card (code 54)", "active", SEED_BALANCE, (1, 2024)),
            ("Low balance card, $25 (code 51)", "active", Decimal("25.00"), EXPIRY),
        ]
        for label, status, balance, (month, year) in specials:
            number = generate_test_card_number(rng)
            test_cards.append(_test_card(label, number, month, year))
            token = tokenize_card(number)
            if token in existing:
                continue
            session.add(
                Card(
                    card_token=token,
                    last4=number[-4:],
                    expiry_month=month,
                    expiry_year=year,
                    status=status,
                    balance=balance,
                    home_lat=40.71,
                    home_lon=-74.0,
                )  # fmt: skip
            )
            counts["cards"] += 1
        await session.commit()

    if test_cards_path is not None:
        test_cards_path.write_text(json.dumps(test_cards, indent=2))
    return counts


def _test_card(label: str, number: str, month: int = EXPIRY[0], year: int = EXPIRY[1]) -> dict:
    return {"label": label, "card_number": number, "expiry_month": month, "expiry_year": year}


async def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=int, default=300, help="number of cardholders")
    parser.add_argument("--csv", default=None, help="path to a Sparkov CSV (default fraudTrain)")
    args = parser.parse_args()

    settings = get_settings()
    csv = Path(args.csv) if args.csv else Path(settings.data_dir) / "fraudTrain.csv"
    if csv.exists():
        print(f"Loading {csv} ...")
        df = pick_cardholders(load_dataset(csv), args.cards)
    else:
        print(f"!! {csv} not found: seeding with FAKER data of the same shape (run `make data`).")
        df = fake_dataset(args.cards)

    await db.connect()
    try:
        counts = await seed(df, Path(settings.test_cards_path))
    finally:
        await db.disconnect()
    print(
        f"Inserted {counts['cards']} cards and {counts['payments']:,} history payments "
        f"({counts['skipped_cards']} cards already existed). "
        f"Test cards written to {settings.test_cards_path}."
    )


if __name__ == "__main__":
    asyncio.run(main())
