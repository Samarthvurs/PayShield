"""Download the Sparkov credit card fraud dataset from Kaggle (`make data`).

Skips the download if the files are already in data/raw/. If the download fails, it prints
what to do and exits normally, so the rest of the project still works (tests use a small
fixture file instead).
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

DATASET = "kartik2112/fraud-detection"
RAW_DIR = Path("data/raw")
FILES = ("fraudTrain.csv", "fraudTest.csv")


def files_present() -> bool:
    """True if both CSV files are already downloaded."""
    return all((RAW_DIR / name).exists() for name in FILES)


def download() -> bool:
    """Run the Kaggle CLI. Returns True on success."""
    if shutil.which("kaggle") is None:
        print("The Kaggle CLI is not installed (pip install kaggle).")
        return False
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cmd = ["kaggle", "datasets", "download", "-d", DATASET, "-p", str(RAW_DIR), "--unzip"]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    return result.returncode == 0 and files_present()


def print_summary() -> None:
    """Print row count and fraud rate for each file."""
    for name in FILES:
        labels = pd.read_csv(RAW_DIR / name, usecols=["is_fraud"])["is_fraud"]
        print(f"{name}: {len(labels):,} rows, {labels.sum():,} fraud ({labels.mean():.3%})")


def main() -> int:
    """Download if needed, then summarise."""
    if files_present():
        print("Dataset already in data/raw/, skipping download.")
    elif not download():
        print(
            "\nDataset download FAILED. To fix it:\n"
            "  1. Create a Kaggle account -> Settings -> 'Create New API Token'.\n"
            "  2. Save kaggle.json to ~/.kaggle/kaggle.json "
            "(Windows: C:\\Users\\<you>\\.kaggle\\kaggle.json).\n"
            "  3. Run `make data` again.\n"
            "Everything else still works; tests use tests/fixtures/sample_transactions.csv."
        )
        return 0
    print_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
