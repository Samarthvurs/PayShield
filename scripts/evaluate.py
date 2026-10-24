"""Evaluate the fraud checker on fraudTest.csv (`make evaluate`) -> docs/evaluation.md.

fraudTest.csv covers LATER dates than the training file, so this is a fair, time-based
test: the model is judged on months it has never seen.

Features for test rows are computed with the card's full earlier history (training months
included), exactly what a live system would have.

Three approaches are compared: rules only, ML only, and combined. We report precision,
recall, F1, PR-AUC and false positive rate. Accuracy is NOT used: with ~0.4% fraud, a
checker that approves everything would be 99.6% "accurate" and catch nothing.
"""

import argparse
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)

from app.config import get_settings  # noqa: E402
from app.fraud.checker import FraudChecker, score_frame  # noqa: E402
from app.fraud.features import LABEL_COLUMN, compute_features, load_dataset  # noqa: E402
from app.fraud.rules import evaluate_rules  # noqa: E402

START, END = "<!-- FRAUD_EVAL_START -->", "<!-- FRAUD_EVAL_END -->"


def metrics(y: np.ndarray, pred: np.ndarray, scores: np.ndarray) -> dict[str, float]:
    """Precision, recall, F1, PR-AUC and false positive rate for one approach."""
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    return {
        "precision": float(precision_score(y, pred, zero_division=0)),
        "recall": float(recall_score(y, pred, zero_division=0)),
        "f1": float(f1_score(y, pred, zero_division=0)),
        "pr_auc": float(average_precision_score(y, scores)) if y.sum() else 0.0,
        "fpr": float(fp / (fp + tn)) if (fp + tn) else 0.0,
        "tp": int(tp), "fp": int(fp), "fn": int(fn), "tn": int(tn),
    }  # fmt: skip


def evaluate(train_csv: Path, test_csv: Path, model_path: Path) -> dict:
    """Score every test row three ways and return all the numbers."""
    train = load_dataset(train_csv) if train_csv.exists() else None
    test = load_dataset(test_csv)
    if train is not None:
        both = pd.concat([train.assign(_test=False), test.assign(_test=True)], ignore_index=True)
    else:
        both = test.assign(_test=True)
    features = compute_features(both)
    is_test = both["_test"].to_numpy()
    features, labels = features[is_test], both.loc[is_test, LABEL_COLUMN].to_numpy()

    checker = FraudChecker.load(model_path)
    scores = score_frame(checker, features)
    rule, ml, final = (scores[c].to_numpy() for c in ("rule_score", "ml_score", "final_score"))

    results = {
        "rows": int(len(labels)),
        "fraud": int(labels.sum()),
        "base_rate": float(labels.mean()),
        "threshold": checker.threshold,
        "ml_threshold": checker.ml_threshold,
        "has_model": checker.has_model,
        "rules_only": metrics(labels, rule > 0, rule),
        "combined": metrics(labels, final >= checker.threshold, final),
    }
    if checker.has_model:
        results["ml_only"] = metrics(labels, ml >= checker.ml_threshold, ml)

    # How often each rule fires, and how often it's right.
    per_rule: dict[str, dict[str, int]] = {}
    for row, label in zip(features.to_dict("records"), labels, strict=True):
        for name in evaluate_rules(row).names:
            stats = per_rule.setdefault(name, {"fired": 0, "fraud": 0})
            stats["fired"] += 1
            stats["fraud"] += int(label)
    results["per_rule"] = per_rule
    results["confusion"] = confusion_matrix(labels, final >= checker.threshold, labels=[0, 1])
    return results


def save_confusion_matrix(cm: np.ndarray, path: Path) -> None:
    """Save the combined checker's confusion matrix as a PNG."""
    fig, ax = plt.subplots(figsize=(4.5, 4))
    ax.imshow(np.log1p(cm), cmap="Blues")
    for (i, j), v in np.ndenumerate(cm):
        ax.text(j, i, f"{v:,}", ha="center", va="center", fontsize=12,
                color="white" if np.log1p(v) > np.log1p(cm).max() / 2 else "black")  # fmt: skip
    ax.set_xticks([0, 1], ["Approved", "Declined"])
    ax.set_yticks([0, 1], ["Legit", "Fraud"])
    ax.set_xlabel("PayShield decision")
    ax.set_ylabel("Truth (dataset label)")
    ax.set_title("Combined checker on fraudTest.csv")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def render(results: dict, test_csv: Path) -> str:
    """The fraud section of docs/evaluation.md."""

    def row(name: str, m: dict) -> str:
        return (
            f"| {name} | {m['precision']:.3f} | {m['recall']:.3f} | {m['f1']:.3f} | "
            f"{m['pr_auc']:.3f} | {m['fpr']:.4%} | {m['tp']:,} | {m['fp']:,} | {m['fn']:,} |"
        )

    lines = [
        "## Fraud checker on the test set",
        "",
        f"Test file: `{test_csv.name}` — **{results['rows']:,} payments, "
        f"{results['fraud']:,} fraud (base rate {results['base_rate']:.3%})**.",
        "",
        "| Approach | Precision | Recall | F1 | PR-AUC | False positive rate | TP | FP | FN |",
        "|---|---|---|---|---|---|---|---|---|",
        row("Rules only (any rule fires)", results["rules_only"]),
    ]
    if "ml_only" in results:
        lines.append(row(f"ML only (score ≥ {results['ml_threshold']:g})", results["ml_only"]))
    lines += [
        row(f"Combined (score ≥ {results['threshold']:g})", results["combined"]),
        "",
        "TP = fraud caught, FP = good payments wrongly declined, FN = fraud missed.",
        "Thresholds were chosen on the validation set (last 10% of training dates), "
        "not on this test set.",
        "",
        "### How often each rule fires",
        "",
        "| Rule | Fired | Of which fraud | Precision |",
        "|---|---|---|---|",
    ]
    for name in ("too_fast", "unusual_amount", "impossible_travel"):
        s = results["per_rule"].get(name, {"fired": 0, "fraud": 0})
        precision = f"{s['fraud'] / s['fired']:.3f}" if s["fired"] else "—"
        lines.append(f"| `{name}` | {s['fired']:,} | {s['fraud']:,} | {precision} |")
    lines += ["", "![Confusion matrix](confusion_matrix.png)", ""]
    if not results["has_model"]:
        lines.insert(2, "> No trained model was found, so ML-only results are missing. "
                        "Run `make train && make evaluate`.\n")  # fmt: skip
    return "\n".join(lines)


LIMITATIONS = """## Limitations

- **Simulated data.** Sparkov is generated by a simulator, not real card traffic. It is widely
  used for teaching and benchmarking, but real fraud is messier and changes over time.
- **No live traffic.** Every number here comes from replaying the dataset offline. Live
  payments in the demo use the current time, so time-based features behave differently.
- **Rules were not tuned to this data.** The three rules use fixed, common-sense limits.
  They are there to be explainable, not to maximise F1.
- **One threshold for everything.** A real issuer would tune it per merchant type and weigh
  the cost of a missed fraud against the cost of annoying a good customer.
"""


def write_section(doc: Path, start: str, end: str, body: str) -> None:
    """Replace the text between two markers in `doc` (creating the file if needed)."""
    text = doc.read_text() if doc.exists() else "# PayShield evaluation\n\n"
    block = f"{start}\n{body}\n{end}"
    if start in text:
        text = re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text)


def main() -> None:
    """Command-line entry point."""
    settings = get_settings()
    raw = Path(settings.data_dir)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-csv", default=str(raw / "fraudTrain.csv"))
    parser.add_argument("--test-csv", default=str(raw / "fraudTest.csv"))
    parser.add_argument("--model", default=settings.model_path)
    parser.add_argument("--doc", default="docs/evaluation.md")
    args = parser.parse_args()

    test_csv, doc = Path(args.test_csv), Path(args.doc)
    if not test_csv.exists():
        body = "## Fraud checker on the test set\n\n**PENDING — run `make data && make train && make evaluate`.**\n"
        write_section(doc, START, END, body + "\n" + LIMITATIONS)
        print(f"{test_csv} not found. Wrote PENDING section to {doc}.")
        return
    print("Evaluating (this takes a few minutes on the full dataset) ...")
    results = evaluate(Path(args.train_csv), test_csv, Path(args.model))
    save_confusion_matrix(results["confusion"], doc.parent / "confusion_matrix.png")
    write_section(doc, START, END, render(results, test_csv) + "\n" + LIMITATIONS)
    for name in ("rules_only", "ml_only", "combined"):
        if name in results:
            m = results[name]
            print(f"{name:11s} precision={m['precision']:.3f} recall={m['recall']:.3f} "
                  f"f1={m['f1']:.3f} pr_auc={m['pr_auc']:.3f} fpr={m['fpr']:.4%}")  # fmt: skip
    print(f"Wrote {doc}")


if __name__ == "__main__":
    main()
