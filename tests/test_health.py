"""Phase 0: the health endpoint reports both databases."""

import httpx


async def test_health_reports_both_databases(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "postgres": "ok", "mongo": "ok"}
