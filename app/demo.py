"""Demo traffic: real transactions from fraudTest.csv, replayed through the payment flow.

Used by the dashboard's "Send demo payments" button (dev only) and by
scripts/demo_traffic.py. Only cardholders that were seeded are used, and each one's test
card number is re-derived from the dataset id (see `test_card_number_for`).
"""

import random
from functools import lru_cache
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.security import SEED_EXPIRY, test_card_number_for, tokenize_card


@lru_cache(maxsize=1)
def _test_rows(path: str) -> pd.DataFrame:
    cols = ["trans_date_trans_time", "cc_num", "merchant", "category", "amt",
            "merch_lat", "merch_long", "is_fraud"]  # fmt: skip
    return pd.read_csv(path, usecols=cols)


def demo_requests(known_tokens: set[str], count: int = 200, seed: int | None = None) -> list[dict]:
    """Up to `count` payment request bodies taken from fraudTest.csv for seeded cards.

    About 15% are dataset fraud rows so the dashboard shows some declines. Payments keep
    their dataset order in time. Raises FileNotFoundError if the dataset is missing.
    """
    path = Path(get_settings().data_dir) / "fraudTest.csv"
    if not path.exists():
        raise FileNotFoundError("fraudTest.csv not found: run `make data` and `make seed` first")
    df = _test_rows(str(path))
    numbers = {cc: test_card_number_for(cc) for cc in df["cc_num"].unique()}
    seeded = {cc for cc, n in numbers.items() if tokenize_card(n) in known_tokens}
    df = df[df["cc_num"].isin(seeded)]
    if df.empty:
        return []

    rng = random.Random(seed)
    fraud, legit = df[df["is_fraud"] == 1], df[df["is_fraud"] == 0]
    n_fraud = min(len(fraud), count * 15 // 100)
    sample = pd.concat([
        fraud.sample(n_fraud, random_state=rng.randint(0, 10**6)),
        legit.sample(min(len(legit), count - n_fraud), random_state=rng.randint(0, 10**6)),
    ]).sort_values("trans_date_trans_time")  # fmt: skip
    return [
        {
            "card_number": numbers[r.cc_num],
            "expiry_month": SEED_EXPIRY[0],
            "expiry_year": SEED_EXPIRY[1],
            "amount": round(float(r.amt), 2),
            "merchant_name": str(r.merchant).removeprefix("fraud_")[:200],
            "merchant_category": r.category,
            "merchant_lat": float(r.merch_lat),
            "merchant_lon": float(r.merch_long),
            "is_fraud": int(r.is_fraud),  # for the demo summary only; never sent to the API
        }
        for r in sample.itertuples(index=False)
    ]
