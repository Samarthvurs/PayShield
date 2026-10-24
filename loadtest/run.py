"""Run the load test and write docs/load_test_results.md (`make loadtest`).

30 users for 60 seconds against the running API. Records payments per second, p50/p95
latency, error rate and the machine specs.
"""

import argparse
import asyncio
import csv
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app import db
from app.demo import demo_requests
from app.models import Card

RESULTS = Path("loadtest/results")


async def prepare_bodies(count: int = 3000) -> int:
    """Write payment bodies for seeded cards to RESULTS/bodies.json."""
    await db.connect()
    try:
        async with db.session_factory()() as s:
            tokens = set((await s.scalars(select(Card.card_token))).all())
    finally:
        await db.disconnect()
    bodies = demo_requests(tokens, count, seed=1)
    for body in bodies:
        body.pop("is_fraud", None)  # dataset label: never sent to the API
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "bodies.json").write_text(json.dumps(bodies))
    return len(bodies)


def machine_specs() -> str:
    """CPU and memory as seen inside the container, plus the host CPU if given."""
    mem_kb = next(
        (int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
         if line.startswith("MemTotal")), 0,
    ) if Path("/proc/meminfo").exists() else 0  # fmt: skip
    host = os.environ.get("HOST_CPU", "not recorded")
    return (
        f"Host CPU: {host}; Docker VM: {os.cpu_count()} vCPUs, {mem_kb / 1024**2:.1f} GB RAM; "
        f"{platform.system()} {platform.release()}; API: Uvicorn (worker count in each run title); "
        "Postgres, MongoDB, API and Locust all on the same machine"
    )


def run_locust(url: str, users: int, seconds: int) -> dict[str, float]:
    """Run Locust headless and read its aggregated CSV stats."""
    cmd = [
        "locust", "-f", "loadtest/locustfile.py", "--headless", "--host", url,
        "-u", str(users), "-r", str(users), "-t", f"{seconds}s",
        "--csv", str(RESULTS / "run"), "--only-summary",
    ]  # fmt: skip
    subprocess.run(cmd, check=False)
    with (RESULTS / "run_stats.csv").open() as f:
        row = next(r for r in csv.DictReader(f) if r["Name"] == "Aggregated")
    requests, failures = int(row["Request Count"]), int(row["Failure Count"])
    return {
        "requests": requests,
        "failures": failures,
        "rps": float(row["Requests/s"]),
        "p50": float(row["50%"]),
        "p95": float(row["95%"]),
        "p99": float(row["99%"]),
        "avg": float(row["Average Response Time"]),
        "error_rate": failures / requests if requests else 0.0,
    }


def render(stats: dict[str, float], users: int, seconds: int, label: str) -> str:
    """One result block for the doc."""
    return (
        f"### {label}\n\n"
        f"_Run at {datetime.now(UTC):%Y-%m-%d %H:%M UTC}, {users} users for {seconds} s._\n\n"
        "| Payments/sec | p50 latency | p95 latency | p99 latency | Requests | Error rate |\n"
        "|---|---|---|---|---|---|\n"
        f"| {stats['rps']:.1f} | {stats['p50']:.0f} ms | {stats['p95']:.0f} ms | "
        f"{stats['p99']:.0f} ms | {int(stats['requests']):,} | {stats['error_rate']:.2%} |\n"
    )


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("PAYSHIELD_URL", "http://api:8000"))
    parser.add_argument("--users", type=int, default=30)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--label", default="Result")
    parser.add_argument("--doc", default="docs/load_test_results.md")
    args = parser.parse_args()

    n = asyncio.run(prepare_bodies())
    if n == 0:
        print("No seeded cards found in fraudTest.csv: run `make data` and `make seed` first.")
        return
    stats = run_locust(args.url, args.users, args.seconds)
    block = render(stats, args.users, args.seconds, args.label)
    doc = Path(args.doc)
    header = (
        "# PayShield load test results\n\n"
        "Command: `make loadtest` (Locust, headless). Each virtual user sends payments "
        "with a unique Idempotency-Key, using real fraudTest.csv transactions for seeded cards. "
        "A declined payment is a valid answer; only HTTP errors count as errors.\n\n"
        f"**Machine:** {machine_specs()}\n\n"
    )
    text = doc.read_text() if doc.exists() else header
    doc.write_text(text.rstrip() + "\n\n" + block)
    print(block)


if __name__ == "__main__":
    main()
