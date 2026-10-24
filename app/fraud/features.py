"""Feature building for the fraud checker.

The SAME function, `compute_features`, is used for training, for evaluation and for live
payments. A live payment is just a tiny table: the card's earlier payments plus the new one.
This avoids "training/serving skew", where the model sees slightly different inputs in
production than it saw while learning.

Every feature uses only the payment itself and the card's EARLIER payments, never later
ones, so training can't peek at the future.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# The 14 merchant categories in the Sparkov dataset.
CATEGORIES: tuple[str, ...] = (
    "entertainment", "food_dining", "gas_transport", "grocery_net", "grocery_pos",
    "health_fitness", "home", "kids_pets", "misc_net", "misc_pos", "personal_care",
    "shopping_net", "shopping_pos", "travel",
)  # fmt: skip
_CATEGORY_CODE = {name: i for i, name in enumerate(CATEGORIES)}

# Inputs the ML model sees. The label (`is_fraud`) is never one of them.
MODEL_FEATURES: list[str] = [
    "amount",
    "amount_zscore",
    "txn_count_1h",
    "txn_count_24h",
    "secs_since_prev",
    "distance_km",
    "speed_kmh",
    "hour",
    "category_code",
]
# Extra values the rules need (not given to the model).
RULE_FEATURES: list[str] = ["prev_count", "txn_count_60s"]
LABEL_COLUMN = "is_fraud"

INPUT_COLUMNS = ["card_id", "time", "amount", "merchant_category", "merchant_lat", "merchant_lon"]
_EARTH_RADIUS_KM = 6371.0


def load_dataset(path: str | Path) -> pd.DataFrame:
    """Load a Sparkov CSV with standard column names.

    Simulated personal data (names, street, date of birth, job...) is dropped straight away;
    only the columns PayShield needs are kept.
    """
    usecols = [
        "trans_date_trans_time", "cc_num", "merchant", "category", "amt",
        "lat", "long", "merch_lat", "merch_long", "is_fraud",
    ]  # fmt: skip
    df = pd.read_csv(path, usecols=usecols)
    df = df.rename(
        columns={
            "trans_date_trans_time": "time",
            "cc_num": "card_id",
            "merchant": "merchant_name",
            "category": "merchant_category",
            "amt": "amount",
            "lat": "home_lat",
            "long": "home_lon",
            "merch_lat": "merchant_lat",
            "merch_long": "merchant_lon",
        }
    )
    df["time"] = pd.to_datetime(df["time"])
    df["merchant_name"] = df["merchant_name"].str.removeprefix("fraud_")  # simulator artefact
    return df


def _haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray):
    """Great-circle distance in km between two sets of points."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build the feature table for every row of `df`.

    `df` needs the columns in INPUT_COLUMNS (one row per payment, any order, many cards).
    Returns a DataFrame with the same index and the columns MODEL_FEATURES + RULE_FEATURES.
    Missing values (e.g. no previous payment) are NaN; the model handles NaN natively.
    """
    d = df[INPUT_COLUMNS].copy()
    d["time"] = pd.to_datetime(d["time"], utc=True).dt.tz_localize(None)
    d["amount"] = d["amount"].astype(float)
    d["_pos"] = np.arange(len(d))
    # Sort by card, then time. Ties keep their original order (stable sort).
    d = d.sort_values(["card_id", "time", "_pos"], kind="mergesort")

    group = d.groupby("card_id", sort=False)
    amount = d["amount"].to_numpy()
    prev_count = group.cumcount().to_numpy().astype(float)

    # Mean and standard deviation of the card's EARLIER amounts (running sums minus this row).
    prev_sum = group["amount"].cumsum().to_numpy() - amount
    prev_sq = (d["amount"] ** 2).groupby(d["card_id"], sort=False).cumsum().to_numpy() - amount**2
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = prev_sum / prev_count
        var = (prev_sq - prev_count * mean**2) / (prev_count - 1)
        std = np.sqrt(np.clip(var, 0, None))
        zscore = np.where((prev_count >= 2) & (std > 0), (amount - mean) / std, 0.0)

    # Count earlier payments inside a time window with a binary search on one sorted key:
    # key = card number * 10^10 + seconds, so different cards never overlap.
    secs = d["time"].to_numpy().astype("datetime64[s]").astype(np.int64)
    card_code = group.ngroup().to_numpy().astype(np.int64)
    key = card_code * 10_000_000_000 + (secs - secs.min() if len(secs) else secs)
    positions = np.arange(len(d))

    def count_in_last(seconds: int) -> np.ndarray:
        start = np.searchsorted(key, key - seconds, side="left")
        return (positions - start).astype(float)

    # Previous payment of the same card (shift by one inside each group).
    prev_secs = group["time"].shift(1)
    first = prev_secs.isna().to_numpy()
    secs_since_prev = (d["time"] - prev_secs).dt.total_seconds().to_numpy()
    prev_lat = group["merchant_lat"].shift(1).to_numpy()
    prev_lon = group["merchant_lon"].shift(1).to_numpy()
    distance = _haversine_km(
        prev_lat, prev_lon, d["merchant_lat"].to_numpy(), d["merchant_lon"].to_numpy()
    )
    # Two payments in the same second count as 1 second apart, to avoid dividing by zero.
    speed = distance / (np.maximum(secs_since_prev, 1.0) / 3600.0)

    out = pd.DataFrame(
        {
            "amount": amount,
            "amount_zscore": zscore,
            "txn_count_1h": count_in_last(3600),
            "txn_count_24h": count_in_last(86400),
            "secs_since_prev": np.where(first, np.nan, secs_since_prev),
            "distance_km": np.where(first, np.nan, distance),
            "speed_kmh": np.where(first, np.nan, speed),
            "hour": d["time"].dt.hour.to_numpy().astype(float),
            "category_code": d["merchant_category"].map(_CATEGORY_CODE).to_numpy(dtype=float),
            "prev_count": prev_count,
            "txn_count_60s": count_in_last(60),
        },
        index=d.index,
    )
    return out.loc[df.index]


def features_for_payment(
    payment: Mapping[str, Any], history: Sequence[Mapping[str, Any]]
) -> dict[str, float | None]:
    """Features for ONE live payment, given the card's earlier payments.

    `payment` and each item of `history` need: time, amount, merchant_category,
    merchant_lat, merchant_lon. Internally this builds a small table and calls
    `compute_features`, the exact code used in training.
    """
    rows = [{**h, "card_id": 0} for h in history] + [{**payment, "card_id": 0}]
    table = pd.DataFrame(rows, columns=INPUT_COLUMNS)
    row = compute_features(table).iloc[-1]
    return {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}


def to_model_row(features: Mapping[str, float | None]) -> list[float]:
    """Order a feature dict as the model expects (None becomes NaN)."""
    return [
        np.nan if features.get(name) is None else float(features[name]) for name in MODEL_FEATURES
    ]


def now_utc() -> datetime:
    """Current time as a naive UTC datetime (all feature times are naive UTC)."""
    return pd.Timestamp.now(tz="UTC").tz_localize(None).to_pydatetime()
