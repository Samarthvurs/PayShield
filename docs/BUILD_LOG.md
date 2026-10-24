# PayShield build log

One entry per phase, written as each phase was finished.

## Phase 0 — Setup & Data ✅

### What was built
- `docker-compose.yml` with three services: `postgres` (pgvector/pgvector:pg16), `mongo` (mongo:7) and `api` (FastAPI). Both databases have healthchecks and named volumes, and the API waits until both are healthy.
- `Dockerfile` (Python 3.11): installs pinned `requirements.txt` and downloads the embedding model at build time.
- `app/config.py` (typed settings from `.env`), `app/db.py` (async Postgres + Mongo connections) and `GET /health`.
- `Makefile` with `up, down, data, seed, train, evaluate, ingest, test, lint, loadtest, demo`, plus `make.ps1` with the same targets for Windows.
- `scripts/download_data.py` (`make data`) downloads the Kaggle dataset and prints row counts and fraud rates.
- `.gitignore`, `.env.example`, git repository on branch `main`.

### How it works (plain English)
`make up` starts three containers. The API container mounts the project folder, so `data/`, `models/` and code are shared with your machine. Every `make` command runs inside the API container (`docker compose run --rm api ...`), so the only things you need installed are Docker and make.

### Key decisions and why
- **Postgres is exposed on host port 5433, not 5432.** This machine already runs a local Postgres on 5432. Inside Docker the API still uses `postgres:5432`.
- **Everything runs in Docker, including tests.** The local Python is 3.14, which several ML libraries don't support yet. The container pins 3.11.
- **CPU-only PyTorch** (needed by sentence-transformers). The CUDA build is about 2 GB bigger and gives no benefit here.
- **The embedding model is downloaded at build time**, so the app and tests work offline.
- **A pip cache mount, a longer timeout and retries in the Dockerfile.** The first build failed on a slow network with a PyPI read timeout. The cache means a retry doesn't download finished packages again.
- **`make.ps1`** because Windows has no `make` by default.
- **Async everywhere** (SQLAlchemy async with psycopg 3, Motor for Mongo), so one request waiting on a database doesn't block the others.

### How to run / check it
```bash
make up          # or .\make.ps1 up
curl localhost:8000/health   # {"status":"ok","postgres":"ok","mongo":"ok"}
make data
```

### Tests (count, what they cover, passing?)
1 test (`tests/test_health.py`): `/health` reports both databases `ok`. **Passing.** Tests use separate `payshield_test` databases, created automatically by `tests/conftest.py`.

Dataset summary from `make data` (real output):
- `fraudTrain.csv`: 1,296,675 rows, 7,506 fraud (0.579%)
- `fraudTest.csv`: 555,719 rows, 2,145 fraud (0.386%)

### Concepts to learn from this phase
Docker Compose services and healthchecks, environment-based configuration (12-factor apps), async I/O, connection pools.

### Limitations
- The API's `--reload` flag is off, so restart the container (`docker compose restart api`) after code changes.
- The Kaggle download over a slow connection stalled once and had to be restarted. The Kaggle CLI resumed it from the partial file.
