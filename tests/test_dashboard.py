"""Phase 4: dashboard page, dev-only helpers and the demo button."""

import json
import shutil
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.fraud.features import load_dataset
from scripts.seed import pick_cardholders, seed

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transactions.csv"
FRONTEND = Path(__file__).parent.parent / "frontend"


async def test_dashboard_page_and_static_files(client: httpx.AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200 and "PayShield" in page.text
    js = await client.get("/static/app.js")
    assert js.status_code == 200


def test_no_api_key_in_committed_frontend() -> None:
    for path in FRONTEND.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "test-api-key" not in text
        assert 'X-API-Key": "' not in text  # the key always comes from user input


@pytest.fixture
def dataset_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app at a data dir whose fraudTest.csv is the fixture file."""
    shutil.copy(FIXTURE, tmp_path / "fraudTest.csv")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_CARDS_PATH", str(tmp_path / "test_cards.json"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_test_cards_and_demo(client: httpx.AsyncClient, dataset_dir: Path) -> None:
    await seed(pick_cardholders(load_dataset(FIXTURE), 6), dataset_dir / "test_cards.json")

    cards = (await client.get("/api/dev/test-cards")).json()
    assert len(cards) == 6 and cards == json.loads((dataset_dir / "test_cards.json").read_text())

    no_key = await client.post("/api/demo?count=50")
    assert no_key.status_code == 401
    r = await client.post("/api/demo?count=50", headers={"X-API-Key": "test-api-key"})
    assert r.status_code == 200
    result = r.json()
    assert result["sent"] == 50 and sum(result["by_code"].values()) == 50
    assert result["dataset_fraud"] > 0

    stats = (await client.get("/api/stats")).json()
    assert stats["total_payments"] == 50


async def test_demo_without_dataset_is_400(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    r = await client.post("/api/demo", headers={"X-API-Key": "test-api-key"})
    get_settings.cache_clear()
    assert r.status_code == 400 and "make data" in r.json()["error"]


async def test_dev_endpoints_hidden_outside_dev(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    cards = await client.get("/api/dev/test-cards")
    demo = await client.post("/api/demo", headers={"X-API-Key": "test-api-key"})
    get_settings.cache_clear()
    assert cards.status_code == 404 and demo.status_code == 404
