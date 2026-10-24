"""Combines the 3 rules and the ML model into one risk score and a decision.

    final_score = 0.5 * rule_score + 0.5 * ml_score      (both 0-100)
    DECLINE if final_score >= threshold

The threshold is chosen on a validation set when the model is trained (best F1) and saved
with the model. If no trained model exists yet, the rules decide alone (final = rule_score)
with a default threshold of 60.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from app.config import get_settings
from app.fraud.features import MODEL_FEATURES
from app.fraud.rules import evaluate_rules

logger = logging.getLogger("payshield.fraud")

DEFAULT_THRESHOLD = 60.0
RULE_WEIGHT = 0.5
ML_WEIGHT = 0.5


@dataclass
class FraudCheckResult:
    """Everything the checker decided about one payment."""

    rule_score: float
    ml_score: float
    final_score: float
    threshold: float
    rules_triggered: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    features: dict[str, float | None] = field(default_factory=dict)

    @property
    def decline(self) -> bool:
        """True if the payment should be declined as suspected fraud."""
        return self.final_score >= self.threshold


class FraudChecker:
    """Holds the trained model (if any) and scores payments."""

    def __init__(self, bundle: Mapping[str, Any] | None = None) -> None:
        self.model = bundle["model"] if bundle else None
        self.threshold: float = float(bundle["threshold"]) if bundle else DEFAULT_THRESHOLD
        self.ml_threshold: float = float(bundle.get("ml_threshold", 50.0)) if bundle else 50.0

    @classmethod
    def load(cls, path: str | Path) -> "FraudChecker":
        """Load the model bundle saved by scripts/train_model.py, or run rules-only."""
        path = Path(path)
        if not path.exists():
            logger.warning("No model at %s: fraud checks use the rules only.", path)
            return cls(None)
        return cls(joblib.load(path))

    @property
    def has_model(self) -> bool:
        """True if a trained ML model is loaded."""
        return self.model is not None

    def ml_scores(self, features: pd.DataFrame) -> np.ndarray:
        """Fraud probability x 100 for each row (0 if there's no model)."""
        if self.model is None:
            return np.zeros(len(features))
        return self.model.predict_proba(features[MODEL_FEATURES])[:, 1] * 100

    def combine(self, rule_score: np.ndarray | float, ml_score: np.ndarray | float):
        """The combined score. Without a model, the rule score is used on its own."""
        if self.model is None:
            return rule_score
        return RULE_WEIGHT * rule_score + ML_WEIGHT * ml_score

    def check(self, features: Mapping[str, float | None]) -> FraudCheckResult:
        """Score one payment from its features."""
        rules = evaluate_rules(features)
        row = pd.DataFrame([{name: features.get(name) for name in MODEL_FEATURES}], dtype=float)
        ml_score = float(self.ml_scores(row)[0])
        final = float(self.combine(float(rules.score), ml_score))
        return FraudCheckResult(
            rule_score=float(rules.score),
            ml_score=round(ml_score, 2),
            final_score=round(final, 2),
            threshold=self.threshold,
            rules_triggered=rules.names,
            reasons=[h.reason for h in rules.hits],
            features=dict(features),
        )


def score_frame(checker: FraudChecker, features: pd.DataFrame) -> pd.DataFrame:
    """Score many rows at once (used by training and evaluation).

    Rules run row by row through the same `evaluate_rules` used for live payments.
    """
    rule_scores = np.array(
        [evaluate_rules(row).score for row in features.to_dict("records")], dtype=float
    )
    ml = checker.ml_scores(features)
    return pd.DataFrame(
        {
            "rule_score": rule_scores,
            "ml_score": ml,
            "final_score": checker.combine(rule_scores, ml),
        },
        index=features.index,
    )


@lru_cache
def get_checker() -> FraudChecker:
    """The app-wide checker, loaded once at startup."""
    return FraudChecker.load(get_settings().model_path)
