"""Train the fraud model (`make train`) -> models/fraud_model.joblib.

Steps:
1. Load fraudTrain.csv and build features with app/fraud/features.py (same code as live).
2. Split by time: the last 10% of dates is the validation set. Training never sees it.
3. Train a HistGradientBoostingClassifier with class weighting (fraud is ~0.6% of rows,
   so without weighting the model would learn to say "not fraud" to everything).
4. On the validation set, pick the decision threshold with the best F1 for the combined
   score (and, for comparison, for the ML score alone). Save model + thresholds together.
"""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import f1_score, precision_score, recall_score

from app.config import get_settings
from app.fraud.checker import DEFAULT_THRESHOLD, FraudChecker, score_frame
from app.fraud.features import LABEL_COLUMN, MODEL_FEATURES, compute_features, load_dataset

THRESHOLDS = np.arange(1.0, 100.0, 0.5)


def time_split(df: pd.DataFrame, valid_fraction: float = 0.1) -> tuple[pd.Series, pd.Series]:
    """Boolean masks (train, valid): rows in the last `valid_fraction` of the time range
    are validation."""
    cutoff = df["time"].quantile(1 - valid_fraction)
    valid = df["time"] > cutoff
    return ~valid, valid


def best_threshold(y: np.ndarray, scores: np.ndarray, default: float) -> tuple[float, float]:
    """The threshold (score >= t means fraud) with the highest F1. Returns (threshold, f1)."""
    if y.sum() == 0:
        return default, 0.0
    best_t, best_f1 = default, -1.0
    for t in THRESHOLDS:
        f1 = f1_score(y, scores >= t, zero_division=0)
        if f1 > best_f1:
            best_t, best_f1 = float(t), float(f1)
    return best_t, best_f1


def build_model() -> HistGradientBoostingClassifier:
    """Gradient boosting: many small decision trees, each one fixing the errors of the
    previous ones. Fast on millions of rows and handles missing values natively."""
    return HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.1,
        max_leaf_nodes=31,
        class_weight="balanced",
        categorical_features=[MODEL_FEATURES.index("category_code")],
        early_stopping=False,
        random_state=42,
    )


def train(csv: Path, model_path: Path) -> dict[str, Any]:
    """Train, choose thresholds, save the bundle. Returns a summary dict."""
    start = time.time()
    df = load_dataset(csv)
    features = compute_features(df)
    assert LABEL_COLUMN not in MODEL_FEATURES  # the answer is never an input
    y = df[LABEL_COLUMN].to_numpy()
    train_mask, valid_mask = time_split(df)

    model = build_model()
    model.fit(features.loc[train_mask, MODEL_FEATURES], y[train_mask])

    checker = FraudChecker({"model": model, "threshold": DEFAULT_THRESHOLD})
    scores = score_frame(checker, features.loc[valid_mask])
    y_valid = y[valid_mask.to_numpy()]
    threshold, f1 = best_threshold(y_valid, scores["final_score"].to_numpy(), DEFAULT_THRESHOLD)
    ml_threshold, ml_f1 = best_threshold(y_valid, scores["ml_score"].to_numpy(), 50.0)

    pred = scores["final_score"].to_numpy() >= threshold
    summary = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "train_csv": str(csv),
        "train_rows": int(train_mask.sum()),
        "valid_rows": int(valid_mask.sum()),
        "train_fraud_rate": float(y[train_mask.to_numpy()].mean()),
        "valid_fraud_rate": float(y_valid.mean()) if len(y_valid) else 0.0,
        "valid_cutoff": str(df.loc[valid_mask, "time"].min()),
        "threshold": threshold,
        "ml_threshold": ml_threshold,
        "valid_f1_combined": f1,
        "valid_f1_ml": ml_f1,
        "valid_precision_combined": float(precision_score(y_valid, pred, zero_division=0)),
        "valid_recall_combined": float(recall_score(y_valid, pred, zero_division=0)),
        "seconds": round(time.time() - start, 1),
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"model": model, "features": MODEL_FEATURES, "threshold": threshold,
         "ml_threshold": ml_threshold, "summary": summary},
        model_path,
    )  # fmt: skip
    return summary


def main() -> None:
    """Command-line entry point."""
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=str(Path(settings.data_dir) / "fraudTrain.csv"))
    parser.add_argument("--out", default=settings.model_path)
    args = parser.parse_args()
    csv = Path(args.csv)
    if not csv.exists():
        print(f"{csv} not found. PENDING: run `make data` first, then `make train`.")
        return
    print(f"Training on {csv} ...")
    summary = train(csv, Path(args.out))
    print(json.dumps(summary, indent=2))
    print(f"Saved model to {args.out}")


if __name__ == "__main__":
    main()
