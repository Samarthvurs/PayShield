"""Send ~200 dataset payments through the running API over HTTP (`make demo`).

Same payments as the dashboard's "Send demo payments" button (see app/demo.py), but sent
as real HTTP requests with an API key and a unique Idempotency-Key each.
"""

import argparse
import asyncio
import os
import uuid
from collections import Counter

import httpx
from sqlalchemy import select

from app import db
from app.config import get_settings
from app.demo import demo_requests
from app.models import Card


async def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--url", default=os.environ.get("PAYSHIELD_URL", "http://api:8000"))
    args = parser.parse_args()

    await db.connect()
    try:
        async with db.session_factory()() as s:
            tokens = set((await s.scalars(select(Card.card_token))).all())
    finally:
        await db.disconnect()
    bodies = demo_requests(tokens, args.count)
    if not bodies:
        print("No seeded cardholders found in fraudTest.csv. Run `make seed` first.")
        return

    codes: Counter[str] = Counter()
    headers = {"X-API-Key": get_settings().api_key}
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        for body in bodies:
            body.pop("is_fraud", None)  # dataset label: never sent to the API
            r = await client.post(
                "/api/payments",
                json=body,
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            )
            codes[r.json().get("response_code", str(r.status_code))] += 1
    print(f"Sent {len(bodies)} payments to {args.url}. Response codes: {dict(codes)}")


if __name__ == "__main__":
    asyncio.run(main())
