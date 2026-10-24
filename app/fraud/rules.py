"""The 3 fraud rules. Each one looks at the features of a payment and may add points.

Rules are easy to explain ("6 payments in a minute") and catch patterns that need no
training data. The ML model catches subtler combinations; the checker combines both.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

TOO_FAST_POINTS = 35
UNUSUAL_AMOUNT_POINTS = 30
IMPOSSIBLE_TRAVEL_POINTS = 35

TOO_FAST_MAX_PAYMENTS = 5  # more than this many in 60 s triggers the rule
UNUSUAL_AMOUNT_ZSCORE = 3.0
UNUSUAL_AMOUNT_MIN_HISTORY = 5
MAX_TRAVEL_KMH = 900.0  # roughly a passenger jet


@dataclass(frozen=True)
class RuleHit:
    """A rule that fired: its name, points and a human-readable reason."""

    name: str
    points: int
    reason: str


@dataclass
class RuleResult:
    """All rules that fired and the combined rule score (0-100)."""

    hits: list[RuleHit] = field(default_factory=list)

    @property
    def score(self) -> int:
        """Sum of points, capped at 100."""
        return min(100, sum(h.points for h in self.hits))

    @property
    def names(self) -> list[str]:
        """Names of the rules that fired."""
        return [h.name for h in self.hits]


def _get(features: Mapping[str, float | None], name: str) -> float | None:
    value = features.get(name)
    return None if value is None or value != value else float(value)  # NaN -> None


def too_fast(features: Mapping[str, float | None]) -> RuleHit | None:
    """More than 5 payments on the card within 60 seconds (this one included).

    Typical of "card testing": a fraudster tries many small payments to see if a stolen
    card works.
    """
    earlier = _get(features, "txn_count_60s") or 0
    total = int(earlier) + 1
    if total > TOO_FAST_MAX_PAYMENTS:
        return RuleHit(
            "too_fast", TOO_FAST_POINTS, f"{total} payments on this card within 60 seconds"
        )
    return None


def unusual_amount(features: Mapping[str, float | None]) -> RuleHit | None:
    """Amount more than 3 standard deviations above the card's usual spending.

    Needs at least 5 earlier payments, otherwise we don't know what "usual" is.
    """
    history = _get(features, "prev_count") or 0
    z = _get(features, "amount_zscore")
    if history >= UNUSUAL_AMOUNT_MIN_HISTORY and z is not None and z > UNUSUAL_AMOUNT_ZSCORE:
        amount = _get(features, "amount") or 0
        return RuleHit(
            "unusual_amount",
            UNUSUAL_AMOUNT_POINTS,
            f"Amount ${amount:,.2f} is {z:.1f} standard deviations above this card's usual spending",
        )
    return None


def impossible_travel(features: Mapping[str, float | None]) -> RuleHit | None:
    """The distance from the previous payment would need travel faster than 900 km/h."""
    speed = _get(features, "speed_kmh")
    if speed is not None and speed > MAX_TRAVEL_KMH:
        distance = _get(features, "distance_km") or 0
        seconds = _get(features, "secs_since_prev") or 0
        gap = f"{seconds:,.0f} seconds" if seconds < 120 else f"{seconds / 60:,.0f} minutes"
        return RuleHit(
            "impossible_travel",
            IMPOSSIBLE_TRAVEL_POINTS,
            f"{distance:,.0f} km from the previous payment only {gap} earlier "
            f"(about {speed:,.0f} km/h)",
        )
    return None


ALL_RULES = (too_fast, unusual_amount, impossible_travel)


def evaluate_rules(features: Mapping[str, float | None]) -> RuleResult:
    """Run every rule and collect the ones that fired."""
    return RuleResult(hits=[hit for rule in ALL_RULES if (hit := rule(features)) is not None])
