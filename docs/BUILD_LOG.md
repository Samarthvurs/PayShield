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

## Phase 1 — Database & Seed Data ✅

### What was built
- **PostgreSQL tables** (`app/models.py`): `cards`, `payments` (index on `(card_id, created_at)` for the fraud checks) and `guide_chunks` (with a `vector(384)` column for RAG).
- **MongoDB collections**: `fraud_checks` and `ai_explanations`, with indexes created at startup (`app/db.py`).
- **`app/security.py`**: Luhn check, Luhn-valid test card generator, HMAC-SHA256 card tokenization.
- **`scripts/seed.py`** (`make seed`): loads 300 dataset cardholders and all their past transactions as payment history, plus 3 special cards (blocked, expired, low balance).
- **`tests/fixtures/sample_transactions.csv`**: 307 made-up rows with the same columns as the Sparkov files (19 fraud), and the script that generates it. Tests use this, never Kaggle.

### How it works (plain English)
The seed reads `fraudTrain.csv`, keeping only the columns PayShield needs; simulated names, addresses and birth dates are dropped straight away. It picks 300 cardholders at random (fixed seed, so it's the same 300 every time). Each one gets a **new** Luhn-valid test card number. That number is turned into a token with HMAC-SHA256 and a server secret, and only the token and last 4 digits are saved. All of the cardholder's past transactions become `payments` rows with `source = "seed"`, so the fraud checker knows each card's normal spending.

### Key decisions and why
- **The test card number is derived from the dataset cardholder id** (`test_card_number_for`). The seed script and the demo traffic both re-create it when needed, so the number is never written to the database.
- **Re-running is safe**: a card whose token already exists is skipped together with its history (second run: "0 cards inserted").
- **Extra `source` column on `payments`** (`seed` vs `api`), which the plan didn't list. Without it the dashboard stats would be swamped by 380k historical rows. Stats and lists only show `api` payments.
- **`card_id` is nullable**, so a payment with an unknown card number can still be recorded (declined, code 14).
- **`is_fraud_label` is stored for seeded history only** and is never read by the fraud checker.
- **Tables are created at startup with `create_all`** instead of migrations (Alembic). That's simpler for a demo; a real system would use migrations.
- The shared dataset loader `load_dataset` lives in `app/fraud/features.py` and arrived in this phase, because the seed needs it.
- **Mongo at seed time:** seeding creates the collections and their indexes. Documents only appear once payments are checked (Phase 3), because a fraud log only makes sense for a payment that was actually checked.

### How to run / check it
```bash
make seed
docker compose exec postgres psql -U payshield -c "select count(*) from cards;"
```
Real output: `Inserted 303 cards and 382,932 history payments` (2,245 of them labelled fraud in the dataset). The second run printed `Inserted 0 cards ... (300 cards already existed)`. The seed took about 2 minutes.

### Tests (count, what they cover, passing?)
3 new tests (`tests/test_seed.py`), 4 total, **all passing**:
- seeding from the fixture inserts the right counts, and re-running inserts nothing;
- **no full card number is stored**: every value in `cards` and `payments` is dumped and searched for every test card number (the last 4 digits are found, the full numbers are not);
- the Faker fallback produces the same columns.

### Concepts to learn from this phase
Tokenization vs encryption (a token can't be reversed without the secret), HMAC, the Luhn checksum, idempotent scripts, why PII is dropped at load time.

### Limitations
- Seeded history keeps the dataset's 2019–2020 dates, so a live payment's "time since previous payment" is large for every card.
- Test card numbers are synthetic and only valid inside PayShield.
