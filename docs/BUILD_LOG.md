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

## Phase 4 — Dashboard ✅

### What was built
A single-page dashboard served by FastAPI at `/` (`frontend/index.html`, `app.js`, `styles.css`; plain HTML, JavaScript and CSS with Chart.js from a CDN):
1. **Stat cards**: total payments, approval rate, fraud-decline rate, average risk score.
2. **Stacked bar chart**: approved vs declined over time (bucket size adapts: minute, hour or day).
3. **Payments table**: last 50, auto-refresh every 5 s, green/red status pills, card last 4, merchant, amount, risk score, triggered rules, and an All / Declined filter.
4. **Test payment form**: pick a seeded test card, amount, merchant, category and location; the result shows inline.
5. **"Send 200 demo payments" button** → `POST /api/demo` (dev only, needs the API key). It replays fraudTest.csv transactions for seeded cards through the same `process_payment` function.
6. A **"Why?" button** on declined rows opens a side panel (wired up in Phase 5).

New endpoints: `GET /`, `/static/*`, `GET /api/dev/test-cards` (dev only), `POST /api/demo` (dev only + API key). `app/demo.py` holds the demo logic shared with `scripts/demo_traffic.py` (`make demo`).

### How it works (plain English)
The page calls the JSON API with `fetch`. It refreshes stats, the chart and the table every 5 seconds. Everything is built with `textContent` (never `innerHTML` with data), so a merchant name can't inject HTML or JavaScript.

### Key decisions and why
- **No API key in the JavaScript.** The user pastes it into a field; it's kept in that browser's `localStorage`. A test scans the frontend files for a hard-coded key.
- **Test cards come from `GET /api/dev/test-cards`**, which reads the git-ignored `scripts/test_cards.json`. It only exists when `APP_ENV=dev`. With any other value both dev endpoints return 404 (tested).
- **The demo uses only seeded cardholders.** Their test card numbers are re-derived from the dataset id, and about 15% of the sample is dataset fraud so the dashboard shows declines. The fraud label is used only for the summary ("N were fraud in the dataset") and is removed before the request is processed.
- **Mobile**: the grid collapses to one column under 860 px, stat cards go 2 per row, and the table scrolls sideways.
- **States**: "Loading…", empty-table and empty-chart messages, readable red error boxes, disabled buttons while sending. If the Chart.js CDN is blocked, the stats and table still work.

### How to run / check it
```bash
make up && make seed && make train
make demo          # or press the button on the dashboard
# open http://localhost:8000
```
Real run of `make demo` against the live API: **200 payments in about 21 s, 186 approved (`00`) and 14 declined as suspected fraud (`59`)**. The dashboard then showed 203 payments (3 from earlier manual tests), an approval rate of 93.1%, a fraud-decline rate of 6.9% and an average risk of 11.4.

Checked in a browser: stats, chart, table, the Declined filter, and the narrow (phone-width) layout. One bug was found and fixed: the chart's "no payments" message stayed visible because a CSS `display` rule overrode the `hidden` attribute. A global `[hidden] { display: none !important; }` fixed it.

### Tests (count, what they cover, passing?)
5 new tests (`tests/test_dashboard.py`), 38 total, **all passing**: the page and static files load; no API key in the frontend; test cards are listed; the demo needs the key and sends 50 fixture payments; a missing dataset gives a readable 400; dev endpoints return 404 outside dev.

### Concepts to learn from this phase
Single-page apps without a framework, polling vs push, XSS and why `textContent` is safe, keeping secrets out of client code, responsive CSS grid.

### Limitations
- Polling every 5 s is simple but wasteful at scale; server-sent events or WebSockets would push updates instead.
- Only the latest 50 payments are shown (the API itself is paginated).

## Phase 5 — AI Explainer (RAG) ✅

### What was built
- **Knowledge base** (`knowledge_base/`): `fraud_rules_guide.md`, `decline_codes.md` and `fraud_patterns.md`, each marked *"Fictional guide for the PayShield demo project"*.
- **Ingest** (`app/ai/ingest.py`, `make ingest`): splits by heading into ≤300-word chunks, embeds them with `all-MiniLM-L6-v2` (384 numbers each, run locally) and stores them in `guide_chunks`. Re-running replaces the old chunks. Real run: **19 chunks**.
- **Retriever** (`app/ai/retriever.py`): builds a query from the response code, the triggered rules and their reasons, then takes the top 3 chunks by cosine distance with pgvector's `<=>` operator.
- **Explainer** (`app/ai/explainer.py`) + `GET /api/payments/{id}/explain`: prompt → Claude → JSON validated with Pydantic, one retry on invalid JSON, a deterministic fallback otherwise, cached in MongoDB `ai_explanations`.
- **Dashboard "Why?" panel** with the summary, reasons and source ids.
- **RAG check** (`scripts/evaluate_rag.py`, part of `make evaluate`): 10 questions, each with the file it should find.

### How it works (plain English)
1. Only **safe facts** are collected, from a whitelist: last 4 digits, amount, category, response code, scores, threshold, rules and reasons. The full card number isn't stored anywhere, so it can't be sent.
2. The query text is turned into a vector, and Postgres finds the 3 guide chunks pointing in the most similar direction.
3. Claude gets the facts and those 3 excerpts, with the instruction to answer **only** from them and reply as JSON `{summary, reasons, sources}`.
4. The reply is validated. Any source id that wasn't one of the 3 excerpts is removed, so citations can't be invented. If the JSON is invalid, Claude is asked once more.
5. With no API key, an API error or a refusal, a template explanation is built from the rule reasons, the scores and the top chunk.
6. The result is cached in Mongo, so each payment is explained (and paid for) once.

### Key decisions and why
- **RAG instead of just asking the LLM.** The model can only use our guide, so it can't make up rules or thresholds, and every answer cites its sources. Changing a rule means editing a Markdown file and re-running `make ingest`, not retraining anything.
- **Local embeddings** (sentence-transformers): free, private, offline, and fast enough for 19 chunks.
- **Model**: `LLM_MODEL` defaults to `claude-opus-5-5`, the current default Claude model. It runs with `effort: low` because this is a short, simple task. Any model id can be set in `.env`.
- **No `ANTHROPIC_API_KEY` was available during the build**, so every explanation produced in this run came from the fallback template (`model: "fallback-template"`). The Claude path is covered by a test with a fake client.
- **A refusal (`stop_reason: "refusal"`) also falls back** to the template instead of showing an error.
- **Only declined payments can be explained** (400 otherwise).
- The threshold was added to the fraud details returned by the API, so the explanation can say "risk 67.34, at or above the threshold of 48.5".

### How to run / check it
```bash
make ingest
make evaluate      # also runs the RAG check
# dashboard: filter Declined -> "Why?"
```
Real results: **RAG hit rate @3 = 100% (10/10)**. In the browser, a declined $392 demo payment showed: risk 67.34 ≥ 48.5, impossible_travel (95 km in seconds), ML score 99.69, three cited guide chunks, and "(cached)" when opened a second time.

### Tests (count, what they cover, passing?)
8 new tests (`tests/test_ai.py`), 46 total, **all passing**: chunking keeps headings and the size limit; re-ingesting replaces chunks; retrieval finds the right file and the hit rate is ≥ 80%; the safe-facts whitelist drops `card_number` and `card_token`; made-up sources are removed; the fallback explanation plus the Mongo cache; approved payments can't be explained; and the **Claude path with a fake client**: one retry after invalid JSON, and **the full card number never appears in what's sent** (only the last 4 digits).

### Concepts to learn from this phase
Embeddings, cosine similarity, vector search (pgvector), chunking, RAG vs fine-tuning, grounding and citations, prompt design for JSON output, validating LLM output, graceful fallbacks, caching.

### Limitations
- The 10 RAG questions are few and were written by the same author as the guide, so 100% is an easy bar. A real evaluation would use questions from real support tickets.
- Fixed in this phase: the impossible_travel reason said "0 minutes earlier" for gaps under a minute; it now says seconds. Explanations cached before the fix keep the old wording.
- Demo payments are sent seconds apart for days-apart dataset transactions, so impossible_travel fires more often in the demo than it would on real-time traffic.

## Phase 6 — Tests, CI & Load Test ✅

### What was built
- `tests/fixtures/sample_transactions.csv` (made in Phase 1): 307 made-up rows with the exact Sparkov columns, 19 fraud. The data tests (seed, features, training, evaluation, demo) run on it, so tests and CI never need Kaggle.
- **Coverage: 95.5% of `app/`** with `pytest-cov` (target 80%). Tests use separate `payshield_test` databases.
- **ruff** for linting and formatting (`make lint`).
- **`.github/workflows/ci.yml`**: on every push and PR, it starts Postgres (pgvector) and MongoDB as service containers, installs dependencies, runs `ruff check`, then `pytest --cov=app --cov-fail-under=80`.
- **Load test** (`loadtest/locustfile.py`, `loadtest/run.py`, `make loadtest`): 30 users for 60 s, each payment with a unique Idempotency-Key and a real fraudTest.csv transaction for a seeded card. Results go to `docs/load_test_results.md`.

### How it works (plain English)
`make loadtest` prepares 3,000 realistic payment bodies, starts Locust headless against the running API, reads Locust's CSV summary and appends payments/sec, p50/p95/p99 latency and error rate to the results file, along with the machine specs.

### Key decisions and why
- **The slowdown found:** about 50 ms of CPU per payment (history 21 ms, pandas features 19 ms, model 9 ms), and one Uvicorn process can only use one core. The index was already there, so this wasn't an index problem.
- **The fix:** 4 Uvicorn workers **and** one math thread per worker (`OMP_NUM_THREADS=1` etc., set only on the API server command, so `make train` still uses every core). Adding workers alone made it 10× worse, because the default thread pools of 4 processes fought over 12 cores. Measured before and after: **21.5 → 98.6 payments/sec, p95 1,900 → 390 ms, 0% errors**.
- **The DB pool was cut to 10 + 5 per worker**, so 4 workers stay under Postgres's 100-connection limit.
- **Coverage needs `concurrency = ["thread", "greenlet"]`.** SQLAlchemy's async layer runs our code in greenlets, and without this setting coverage under-reported `payments.py` as 44% when it's really about 91%.
- **A new test checks that a MongoDB outage doesn't fail a payment**: the payment commits in Postgres and the logging error is swallowed and logged.
- **ruff ignores E501 (line length)** because `ruff format` already handles it, as ruff's docs recommend.

### How to run / check it
```bash
make test       # 47 tests + coverage
make lint
make loadtest   # needs `make up`, `make seed` and the dataset
```

### Tests (count, what they cover, passing?)
**47 tests, all passing**, 95.5% coverage of `app/`. 1 new test this phase (Mongo outage).
**CI:** the workflow file parses and mirrors the local commands, but it has **not run on GitHub yet**: this repo has no remote. It will run on the first push.

### Concepts to learn from this phase
Test fixtures vs real data, coverage (and its blind spots), CI service containers, load testing, percentiles (p50/p95/p99), the CPU-bound vs I/O-bound split, the event loop, process vs thread parallelism, thread oversubscription, connection-pool sizing.

### Limitations
- Locust, the API and both databases share one laptop, so the numbers are a lower bound, not a capacity plan.
- Fetching a card's full history per payment is the main remaining cost. At real scale you'd keep running stats per card (count, mean, variance, last location) and update them on each payment.

## Phase 7 — README & Final Explanation ✅

### What was built
- **`README.md`**: one-line description, the 4-part diagram, a results table with real numbers only, how to run, API endpoints with `curl` examples, why each tech choice (especially Postgres vs MongoDB), dataset credit, a "Built with Claude Code" section, limitations and future ideas.
- **`docs/PROJECT_EXPLAINED.md`**: one payment from start to finish, the 4 parts, every table and collection, the fraud checker and what each metric means, RAG step by step, a glossary, the Visa JD mapping, 10 interview questions with answers, 3 resume bullets and a 2-minute demo script.

### How it works (plain English)
Every number in both documents was copied from a script's output: `make evaluate` (`docs/evaluation.md`), `make loadtest` (`docs/load_test_results.md`), `make test`, `make seed` and `make demo`.

### Key decisions and why
- **The weaker-than-ML combined score is shown openly** in the README results, with the reason. Measuring and explaining a surprising result is worth more in an interview than hiding it.
- The demo script was checked against the live API: the blocked card → `05`, the low-balance card with $50 → `51`, and the expired card → `54`.

### How to run / check it
Read `README.md` → "How to run".

### Tests (count, what they cover, passing?)
No code changes in this phase. 47 tests, all passing.

### Concepts to learn from this phase
Writing for your audience, reproducible results, honest reporting of limitations.

### Limitations
- The GitHub Actions workflow hasn't run yet because the repository has no remote. It runs on the first push.
- Explanations in this run came from the fallback template (no `ANTHROPIC_API_KEY` was set).

## Change — AI explainer switched from Claude to Google Gemini ✅

### What changed
- `app/ai/explainer.py` now calls the **Google Gemini API** through the official `google-genai` SDK (`client.aio.models.generate_content`), replacing the Anthropic SDK.
- `.env`: `ANTHROPIC_API_KEY` → **`GEMINI_API_KEY`**, and `LLM_MODEL` defaults to **`gemini-3.5-flash`**.
- `requirements.txt`: `anthropic` → `google-genai==2.28.0`, and `pydantic` 2.10.4 → **2.12.5** (required by that SDK version).
- Tests, README and `PROJECT_EXPLAINED.md` were updated. Earlier entries in this log are left as they were written.

### How it works (plain English)
The RAG steps didn't change: safe facts only, the 3 nearest guide chunks, validation, one retry, the fallback and the Mongo cache. Only the "ask the LLM" step is different:
- **JSON mode**: `response_mime_type="application/json"` plus `response_schema=Explanation` (the same Pydantic model we validate with), so Gemini replies with JSON directly. We still validate it and drop invented source ids.
- `temperature=0.2`, so explanations are steady rather than creative.
- If a **safety filter blocks** the reply, `response.text` is empty, and PayShield uses the template fallback. An **API or network error** (`google.genai.errors.APIError`, `httpx.HTTPError`) also falls back.

### Key decisions and why
- **`gemini-3.5-flash`**: a fast, cheap model is enough for a short explanation, and it's the current Flash model used in the SDK's own tests. Any model can be set with `LLM_MODEL`.
- **SDK version 2.28.0**, a few releases behind the newest (2.33.0), because a release that has been out a while is safer to pin.

### How to run / check it
Put `GEMINI_API_KEY=...` in `.env` (free key from Google AI Studio), run `docker compose up -d --build`, then click **Why?** on a declined payment.

**Real live result** (payment `bdf47ef7…`, $760.29, risk 51.05 ≥ 48.5): `model: "gemini-3.5-flash"`, with three reasons (13 payments in 60 s → `too_fast`; 106 km in 11 s → `impossible_travel`; 6.0 standard deviations above usual spending), each grounded in the cited chunks `decline_codes.md#6`, `fraud_rules_guide.md#2` and `fraud_rules_guide.md#4`.

### Tests (count, what they cover, passing?)
**49 tests, all passing; coverage 96.1%.** The Gemini path is tested with a fake client: it retries once after invalid JSON, uses JSON mode, and **never sends the full card number** (only the last 4 digits). New: a blocked reply and an API error (503) both fall back to the template.

### Limitations
- Only one live Gemini call was made during the build. Explanation quality hasn't been measured beyond the retrieval hit rate.
