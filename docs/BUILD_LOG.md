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

## Phase 2 — Payment API ✅

### What was built
- `POST /api/payments` (headers `X-API-Key` and `Idempotency-Key`), returning `payment_id, status, response_code, message, risk_score`.
- `GET /api/payments` (newest first, `status` filter, pagination), `GET /api/payments/{id}` and `GET /api/stats` (totals, approval rate, fraud-decline rate, average risk, payments over time).
- `app/payments.py`: the payment flow. `app/schemas.py`: Pydantic request and response models.
- Clean JSON errors: `{"error": "..."}` for 401/404, a 422 that lists field names and messages only, and a plain 500 with no stack trace.

### How it works (plain English)
The flow is one database transaction, with checks in this order:
1. **Idempotency.** If this `Idempotency-Key` was used before, return the saved answer. A retry never charges twice.
2. **Luhn check** on the number → `14` if it fails.
3. **Tokenize and look up** the card → `14` if unknown (or wrong expiry), `05` if blocked, `54` if expired.
4. **Lock the card row** (`SELECT ... FOR UPDATE`, done in the same query as the lookup). A second payment on the same card waits here until the first commits.
5. **Balance** → `51` if too low.
6. **Fraud check** → `59` if the score reaches the threshold. In this phase it's a placeholder that always returns 0.
7. Approve (`00`), deduct the balance, save the payment and commit, all together.

### Key decisions and why
- **Declined payments are saved too**, so the dashboard shows them and the idempotent replay works for declines.
- **Two requests racing with the same Idempotency-Key**: both may pass step 1, but the `UNIQUE` constraint on `idempotency_key` lets only one insert succeed. The loser catches the `IntegrityError` and returns the winner's saved answer.
- **The request's expiry must match the card's stored expiry**, otherwise `14`. A card is valid until the end of its expiry month.
- **Custom 422 handler.** FastAPI's default validation error echoes the submitted input, which could include a full card number. Ours only returns field names and messages. `card_number` also has `repr=False`, so it never shows up in logs if the object is printed.
- **The API key is compared with `hmac.compare_digest`**, so the response time doesn't leak how much of the key was right.
- **`merchant_category` must be one of the 14 dataset categories**, because the model only knows those.
- **`GET` endpoints don't need the API key.** The dashboard is read-only and local. Only creating payments (and, later, the demo button) needs the key. A real deployment would put auth on the reads too.
- **The stats chart's bucket size adapts** to the time span (minute / hour / day).
- **`Idempotency-Key` is required**; a missing key is a 422. Reusing a key with a different body returns the first saved answer (no body comparison, kept simple).

### How to run / check it
```bash
curl -X POST localhost:8000/api/payments -H "X-API-Key: $API_KEY" -H "Idempotency-Key: demo-1" \
  -H "Content-Type: application/json" \
  -d '{"card_number":"<from scripts/test_cards.json>","expiry_month":12,"expiry_year":2030,"amount":24.99,"merchant_name":"Store 42","merchant_category":"grocery_pos","merchant_lat":40.7,"merchant_lon":-74.0}'
```
Checked live: sending the same request twice returned the same `payment_id` (one charge), and a wrong key returned `{"error":"Invalid API key"}` (401).

### Tests (count, what they cover, passing?)
15 new tests (`tests/test_payments.py`), 19 total, **all passing**: approval and balance deduction; codes 14 (bad Luhn, unknown card, wrong expiry), 05, 54 and 51; idempotent retry charges once; **5 simultaneous retries with the same key** create one payment; **two simultaneous $60 payments on a $100 card** give exactly one `00` and one `51` (balance ends at $40); wrong API key → 401; missing Idempotency-Key → 422; the validation error never contains the card number; list, filter, get, 404 and stats.

### Concepts to learn from this phase
Idempotency keys, ACID transactions, pessimistic row locking (`FOR UPDATE`), race conditions, ISO 8583-style response codes, unique constraints as a concurrency guard, never leaking input in errors.

### Limitations
- Balances are a simple prepaid-style number. Real card networks separate authorization from settlement (clearing).
- There's no rate limiting.

## Phase 3 — Fraud Checker (ML + 3 Rules) ✅

### What was built
- `app/fraud/features.py`: **one** feature function (`compute_features`) used by training, evaluation and live payments.
- `app/fraud/rules.py`: `too_fast` (35), `unusual_amount` (30), `impossible_travel` (35), each returning a human-readable reason.
- `app/fraud/checker.py`: `final = 0.5 × rule_score + 0.5 × ml_score`, decline if `final ≥ threshold`. It's loaded once at app startup.
- `scripts/train_model.py` (`make train`) and `scripts/evaluate.py` (`make evaluate`) → `docs/evaluation.md` + `docs/confusion_matrix.png`.
- Every live check is logged to MongoDB `fraud_checks` (scores, rules, reasons, features, last 4 digits).

### How it works (plain English)
For a live payment, the API loads the card's earlier payments (using the `(card_id, created_at)` index), adds the new payment as the last row of a small table, and runs the same `compute_features` used in training. Features use only the past: amount, z-score against earlier amounts, number of payments in the last 60 s / 1 h / 24 h, seconds since the previous payment, distance and speed from the previous payment, hour, category. The rules turn these into points, the model turns them into a fraud probability, and the checker combines the two.

### Key decisions and why
- **Same code for training and live** (`features_for_payment` wraps `compute_features`). A test checks that live features equal training features row by row.
- **Vectorized features.** Running per-row Python over 1.85M rows would be far too slow, so time windows use one binary search (`np.searchsorted`) over a sorted `card × time` key. Feature building plus training on 1.3M rows took **35 s**.
- **Time-based split**: the last 10% of training dates (from 2020-05-02) is the validation set. Training: 1,167,007 rows; validation: 129,668 rows.
- **`class_weight="balanced"`**, because fraud is only 0.57% of training rows.
- **Thresholds chosen on validation by best F1**: combined **48.5**, ML-only **98.0** (balanced weighting pushes probabilities up, hence the high ML threshold). No refit on the full training set after choosing them, kept simple.
- **Without a model** the rules decide alone with threshold 60, so a single rule (max 35) can't decline but two together (70) can.
- **Evaluation history:** test-set features are computed with each card's earlier history, including the training months, as a live system would have.
- **Mongo logging happens after the Postgres commit and never fails the payment.** If Mongo is down, the payment still goes through and the error is logged.
- Label safety: `is_fraud` isn't in `MODEL_FEATURES`, and a test flips every label and checks the features don't change.

### How to run / check it
```bash
make train      # ~40 s
make evaluate   # ~40 s
```

### Results (real output of `make evaluate` on fraudTest.csv: 555,719 rows, 2,145 fraud, base rate 0.386%)
| Approach | Precision | Recall | F1 | PR-AUC | FPR |
|---|---|---|---|---|---|
| Rules only | 0.047 | 0.467 | 0.085 | 0.038 | 3.68% |
| ML only (≥ 98) | 0.781 | 0.765 | **0.772** | **0.848** | 0.083% |
| Combined (≥ 48.5) | 0.538 | **0.839** | 0.656 | 0.611 | 0.279% |

**What this means, honestly:**
- The ML model does most of the work. The plan's fixed 50/50 combination **catches more fraud** (1,799 vs 1,640, recall 0.84 vs 0.77) but **wrongly declines about 3× more good payments** (1,542 vs 461), so its F1 is lower than ML alone.
- `too_fast` **never fires** on Sparkov: the simulator has no bursts of 6 payments in a minute. It's still useful for live card-testing attacks, which a test demonstrates.
- `impossible_travel` fires 14,176 times but only 190 are fraud. Sparkov places each merchant randomly around the cardholder's home, so two quick payments can look "far apart". On simulated data this rule mostly adds noise.
- `unusual_amount` is the most useful rule (precision 0.12, about 30× the base rate).
- Live check: a $1,450 payment on a card that usually spends small amounts fired `unusual_amount` (z = 9.3), got ML score 37 and final 33.65, below 48.5, so it was approved and logged to Mongo.

### Tests (count, what they cover, passing?)
14 new tests (`tests/test_fraud.py`), 33 total, **all passing**: each rule fires and doesn't fire (including the exact boundaries 5 vs 6 payments, z = 3.0, 900 km/h); the score caps at 100; the **threshold boundary** (exactly 50.0 declines, 49.5 doesn't); rules-only mode; live features = training features; features use only earlier rows; the **label never affects features**; time split; training and evaluation on the fixture; and a live card-testing burst from far away → `59` with risk 70, logged to Mongo without the card number.

### Concepts to learn from this phase
Training/serving skew, time-based validation (no peeking at the future), class imbalance, gradient boosting, precision vs recall vs F1, PR-AUC (better than ROC-AUC for rare classes), false positive rate, why accuracy is useless here (approve everything = 99.6% "accurate").

### Limitations
- The 50/50 weights are fixed by design. Learning the weights, or using the ML score alone with rules as explanations, would score better on this data.
- Live payments use the current UTC time for "hour of day", while the dataset uses simulated local time.
- Fetching a card's whole history on each payment is fine for about 1,300 rows per card, but would need a cap or pre-aggregated stats at real scale.
