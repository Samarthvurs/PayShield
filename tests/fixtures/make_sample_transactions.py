"""Generate tests/fixtures/sample_transactions.csv: ~300 made-up rows, Sparkov columns.

Everything here is invented (no real people). Run: python tests/fixtures/make_sample_transactions.py
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

COLUMNS = [
    "", "trans_date_trans_time", "cc_num", "merchant", "category", "amt", "first", "last",
    "gender", "street", "city", "state", "zip", "lat", "long", "city_pop", "job", "dob",
    "trans_num", "unix_time", "merch_lat", "merch_long", "is_fraud",
]  # fmt: skip
CATEGORIES = [
    "grocery_pos", "gas_transport", "home", "shopping_pos", "kids_pets", "food_dining",
    "personal_care", "health_fitness", "entertainment", "misc_pos",
]  # fmt: skip
FRAUD_CATEGORIES = ["shopping_net", "misc_net", "grocery_pos"]


def main() -> None:
    rng = random.Random(7)
    rows: list[list] = []
    start = datetime(2019, 1, 1, 8, 0, 0)
    for c in range(6):
        cc_num = 4_100_000_000_000_000 + c * 1111
        lat, lon = 30 + c * 2.5, -100 + c * 3.0
        t = start + timedelta(hours=c)

        def add(time: datetime, cat: str, amt: float, fraud: int, mlat: float, mlon: float):
            rows.append([
                time.strftime("%Y-%m-%d %H:%M:%S"), cc_num, f"fraud_Shop {rng.randint(1, 40)}",
                cat, round(amt, 2), "Test", f"Person{c}", "F" if c % 2 else "M",
                f"{c} Fake St", "Testville", "TX", 70000 + c, lat, lon, 10000, "Tester",
                "1980-01-01", f"t{len(rows):05d}", int(time.timestamp()), mlat, mlon, fraud,
            ])  # fmt: skip

        for _ in range(45):
            t += timedelta(hours=rng.uniform(20, 90))
            add(t, rng.choice(CATEGORIES), rng.lognormvariate(3.5, 0.6), 0,
                lat + rng.uniform(-0.5, 0.5), lon + rng.uniform(-0.5, 0.5))  # fmt: skip
        if c in (1, 3, 5):  # a fraud burst at night: big online purchases
            t = t.replace(hour=23, minute=0) + timedelta(days=1)
            for _ in range(4):
                t += timedelta(minutes=rng.uniform(5, 40))
                add(t, rng.choice(FRAUD_CATEGORIES), rng.uniform(700, 1200), 1,
                    lat + rng.uniform(-0.5, 0.5), lon + rng.uniform(-0.5, 0.5))  # fmt: skip
        if c == 3:  # card testing: 6 small payments within a minute
            t += timedelta(hours=2)
            for _ in range(6):
                t += timedelta(seconds=8)
                add(t, "misc_net", rng.uniform(1, 5), 1, lat, lon)
        if c == 5:  # impossible travel: 2,000+ km away 10 minutes later
            t += timedelta(minutes=10)
            add(t, "shopping_net", 950.0, 1, lat + 20, lon + 10)
        for _ in range(3):
            t += timedelta(hours=rng.uniform(20, 90))
            add(t, rng.choice(CATEGORIES), rng.lognormvariate(3.5, 0.6), 0,
                lat + rng.uniform(-0.5, 0.5), lon + rng.uniform(-0.5, 0.5))  # fmt: skip

    rows.sort(key=lambda r: r[0])
    out = Path(__file__).with_name("sample_transactions.csv")
    with out.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for i, row in enumerate(rows):
            writer.writerow([i, *row])
    print(f"Wrote {len(rows)} rows ({sum(r[-1] for r in rows)} fraud) to {out}")


if __name__ == "__main__":
    main()
