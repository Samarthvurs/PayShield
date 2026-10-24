"""Phase 3: features, rules, the combined checker, training and evaluation."""

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from app import db
from app.fraud.checker import FraudChecker, score_frame
from app.fraud.features import (
    LABEL_COLUMN,
    MODEL_FEATURES,
    compute_features,
    features_for_payment,
    load_dataset,
    now_utc,
)
from app.fraud.rules import evaluate_rules, impossible_travel, too_fast, unusual_amount
from scripts.evaluate import evaluate, render, write_section
from scripts.train_model import best_threshold, time_split, train
from tests.helpers import add_history, headers, make_card, payment_body

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transactions.csv"


# ---------- Rules ----------


def test_too_fast_fires_on_sixth_payment_in_a_minute() -> None:
    assert too_fast({"txn_count_60s": 5}) is not None  # 5 earlier + this one = 6
    assert too_fast({"txn_count_60s": 4}) is None  # exactly 5 is allowed
    assert too_fast({}) is None


def test_unusual_amount_needs_history_and_high_zscore() -> None:
    hit = unusual_amount({"prev_count": 5, "amount_zscore": 3.5, "amount": 900})
    assert hit is not None and hit.points == 30 and "$900.00" in hit.reason
    assert unusual_amount({"prev_count": 4, "amount_zscore": 10, "amount": 900}) is None
    assert unusual_amount({"prev_count": 50, "amount_zscore": 3.0, "amount": 900}) is None


def test_impossible_travel() -> None:
    assert (
        impossible_travel({"speed_kmh": 901, "distance_km": 1000, "secs_since_prev": 60})
        is not None
    )
    assert impossible_travel({"speed_kmh": 899}) is None
    assert impossible_travel({"speed_kmh": None}) is None
    assert impossible_travel({"speed_kmh": float("nan")}) is None


def test_rule_score_is_capped_at_100() -> None:
    features = {
        "txn_count_60s": 9,
        "prev_count": 9,
        "amount_zscore": 9,
        "amount": 1,
        "speed_kmh": 5000,
    }
    result = evaluate_rules(features)
    assert result.names == ["too_fast", "unusual_amount", "impossible_travel"]
    assert result.score == 100  # 35 + 30 + 35


# ---------- Combined checker ----------


class FixedModel:
    """A stand-in model that always returns the same fraud probability."""

    def __init__(self, p: float) -> None:
        self.p = p

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        return np.tile([1 - self.p, self.p], (len(x), 1))


@pytest.mark.parametrize(("p", "declined"), [(0.70, True), (0.69, False)])
def test_threshold_boundary(p: float, declined: bool) -> None:
    checker = FraudChecker({"model": FixedModel(p), "threshold": 50.0})
    # unusual_amount gives rule_score 30: 0.5*30 + 0.5*70 = 50.0 -> exactly the threshold
    result = checker.check({"prev_count": 10, "amount_zscore": 4, "amount": 100})
    assert result.rule_score == 30
    assert result.decline is declined


def test_no_model_means_rules_only_with_default_threshold() -> None:
    checker = FraudChecker.load("models/does-not-exist.joblib")
    assert not checker.has_model and checker.threshold == 60
    two_rules = checker.check({"txn_count_60s": 9, "speed_kmh": 5000})
    assert two_rules.final_score == 70 and two_rules.decline
    assert not checker.check({"txn_count_60s": 9}).decline  # one rule (35) isn't enough


# ---------- Features ----------


def test_live_features_match_training_features() -> None:
    df = load_dataset(FIXTURE)
    batch = compute_features(df)
    card = df[df["card_id"] == df["card_id"].iloc[0]].sort_values("time")
    for pos in (0, 1, 10, len(card) - 1):
        target = card.iloc[pos]
        history = card.iloc[:pos].to_dict("records")
        live = features_for_payment(target.to_dict(), history)
        expected = batch.loc[card.index[pos]]
        for name in [*MODEL_FEATURES, "prev_count", "txn_count_60s"]:
            if pd.isna(expected[name]):
                assert live[name] is None, name
            else:
                assert live[name] == pytest.approx(expected[name]), name


def test_features_use_only_earlier_payments() -> None:
    t0 = datetime(2020, 1, 1, 12)
    rows = [
        {"card_id": 1, "time": t0 + timedelta(seconds=10 * i), "amount": 10.0 * (i + 1),
         "merchant_category": "grocery_pos", "merchant_lat": 40.0, "merchant_lon": -74.0}
        for i in range(7)
    ]  # fmt: skip
    f = compute_features(pd.DataFrame(rows))
    assert f["prev_count"].tolist() == [0, 1, 2, 3, 4, 5, 6]
    assert f["txn_count_60s"].tolist() == [0, 1, 2, 3, 4, 5, 6]
    assert f["secs_since_prev"].iloc[0] != f["secs_since_prev"].iloc[0]  # NaN for the first
    assert f["secs_since_prev"].iloc[1] == 10
    assert f["category_code"].iloc[0] == 4  # grocery_pos


def test_label_is_never_a_feature() -> None:
    assert LABEL_COLUMN not in MODEL_FEATURES
    df = load_dataset(FIXTURE)
    features = compute_features(df)
    assert LABEL_COLUMN not in features.columns
    flipped = compute_features(df.assign(is_fraud=1 - df["is_fraud"]))
    pd.testing.assert_frame_equal(features, flipped)  # changing labels changes nothing


# ---------- Training and evaluation on the fixture ----------


def test_time_split_and_best_threshold() -> None:
    df = load_dataset(FIXTURE)
    train_mask, valid_mask = time_split(df)
    assert df.loc[train_mask, "time"].max() <= df.loc[valid_mask, "time"].min()
    y = np.array([0, 0, 1, 1])
    assert best_threshold(y, np.array([10, 20, 80, 90]), 60)[1] == 1.0
    assert best_threshold(np.zeros(3), np.zeros(3), 60) == (60, 0.0)


def test_train_and_evaluate_on_fixture(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    summary = train(FIXTURE, model_path)
    assert model_path.exists() and summary["train_rows"] > summary["valid_rows"] > 0

    checker = FraudChecker.load(model_path)
    assert checker.has_model
    scores = score_frame(checker, compute_features(load_dataset(FIXTURE)))
    assert scores["ml_score"].between(0, 100).all()

    results = evaluate(tmp_path / "no-train.csv", FIXTURE, model_path)
    assert results["rows"] == 307 and results["fraud"] == 19
    assert {"rules_only", "ml_only", "combined"} <= results.keys()
    doc = tmp_path / "evaluation.md"
    write_section(doc, "<!-- A -->", "<!-- B -->", render(results, FIXTURE))
    write_section(doc, "<!-- A -->", "<!-- B -->", render(results, FIXTURE))  # replaces, no dup
    assert doc.read_text().count("Rules only") == 1


# ---------- Live payments go through the checker ----------


async def test_card_testing_burst_from_far_away_is_declined_59(client: httpx.AsyncClient) -> None:
    number, card_id = await make_card(balance="1000.00")
    now = now_utc()
    # 5 payments in New York during the last 50 seconds...
    await add_history(
        card_id, [(now - timedelta(seconds=50 - 10 * i), 2.0, 40.71, -74.0) for i in range(5)]
    )
    # ...then one in Los Angeles: too_fast (35) + impossible_travel (35) = 70 >= 60
    body = payment_body(number, 3.0, merchant_lat=34.05, merchant_lon=-118.24)
    r = await client.post("/api/payments", json=body, headers=headers())
    assert r.json()["response_code"] == "59"
    assert r.json()["risk_score"] == 70

    log = await db.get_mongo().fraud_checks.find_one({"payment_id": r.json()["payment_id"]})
    assert log["decision"] == "DECLINE"
    assert log["rules_triggered"] == ["too_fast", "impossible_travel"]
    assert log["card_last4"] == number[-4:]
    assert number not in str(log)

    detail = (await client.get(f"/api/payments/{r.json()['payment_id']}")).json()
    assert detail["rules_triggered"] == ["too_fast", "impossible_travel"]
    assert detail["fraud"]["final_score"] == 70


async def test_normal_payment_is_approved_and_logged(client: httpx.AsyncClient) -> None:
    number, _ = await make_card()
    r = await client.post("/api/payments", json=payment_body(number), headers=headers())
    assert r.json()["response_code"] == "00"
    log = await db.get_mongo().fraud_checks.find_one({"payment_id": r.json()["payment_id"]})
    assert log["decision"] == "APPROVE" and log["rules_triggered"] == []
