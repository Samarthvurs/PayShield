"""Locust load test: virtual users sending payments, each with a unique Idempotency-Key.

Payment bodies come from loadtest/results/bodies.json (written by loadtest/run.py), so the
load test uses many different seeded cards, like real traffic.
"""

import json
import os
import random
import uuid
from pathlib import Path

from locust import HttpUser, between, task

BODIES = json.loads(Path(os.environ.get("BODIES_FILE", "loadtest/results/bodies.json")).read_text())
API_KEY = os.environ["API_KEY"]


class PaymentUser(HttpUser):
    """One simulated client sending payments with a short pause in between."""

    wait_time = between(0.05, 0.2)

    @task
    def pay(self) -> None:
        body = random.choice(BODIES)
        headers = {"X-API-Key": API_KEY, "Idempotency-Key": str(uuid.uuid4())}
        with self.client.post("/api/payments", json=body, headers=headers, name="POST /api/payments",
                              catch_response=True) as response:  # fmt: skip
            # A decline is a valid answer; only HTTP errors count as failures.
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
