# PayShield

**A payment API that checks every payment for fraud in real time, with a dashboard that shows the results and uses AI to explain why a payment was declined.**

```
Payment request  →  Payment API  →  Fraud check (ML model + 3 rules)  →  APPROVED / DECLINED
                         │                      │
                    PostgreSQL              MongoDB
                 (cards, payments)      (fraud check logs, AI explanations)
                         │
          Dashboard  →  "Why was this declined?"  →  AI explanation (RAG)
```

| # | Part | What it does | Tech |
|---|---|---|---|
| 1 | **Payment API** | Takes a payment, checks the card and balance, returns APPROVED/DECLINED | Python, FastAPI, PostgreSQL |
| 2 | **Fraud checker** | ML model trained on a public fraud dataset + 3 simple rules → one risk score | scikit-learn, MongoDB for logs |
| 3 | **Dashboard** | Payments table, approve/decline chart, fraud rate, test-payment form | HTML, JavaScript, CSS, Chart.js |
| 4 | **AI explainer** | Explains a declined payment in plain English, grounded in a fraud-rules guide | RAG (pgvector) + Google Gemini API |

## Results

Every number below comes from a script that was run; the full output is in [`docs/`](docs/).

**Fraud detection** on `fraudTest.csv`: 555,719 payments from months the model never saw, 2,145 fraud (base rate 0.386%). Source: [`docs/evaluation.md`](docs/evaluation.md).

| Approach | Precision | Recall | F1 | PR-AUC | False positive rate |
|---|---|---|---|---|---|
| Rules only | 0.047 | 0.467 | 0.085 | 0.038 | 3.68% |
| ML only | **0.781** | 0.765 | **0.772** | **0.848** | **0.083%** |
| Combined (0.5 × rules + 0.5 × ML) | 0.538 | **0.839** | 0.656 | 0.611 | 0.279% |

The combined score catches the most fraud (1,799 of 2,145), but declines 3× more good payments than ML alone. On this simulated data the ML model does most of the work. See [Limitations](#limitations-and-future-ideas).

| Other results | Value | Source |
|---|---|---|
| RAG retrieval hit rate @3 | **100%** (10/10 questions) | [`docs/evaluation.md`](docs/evaluation.md) |
| Load test throughput | **98.6 payments/sec** (30 users, 60 s, 0% errors) | [`docs/load_test_results.md`](docs/load_test_results.md) |
| Latency | **p50 150 ms, p95 390 ms** | same |
| Tests | **49 passing, 96.1% coverage** of `app/` | `make test` |

## How to run

You need **Docker Desktop** and a free **Kaggle** account. `make` is optional: on Windows use `.\make.ps1 <target>` with the same target names.

1. **Kaggle token:** go to kaggle.com → *Settings* → *Create New API Token*, then save `kaggle.json` to `~/.kaggle/kaggle.json` (Windows: `C:\Users\<you>\.kaggle\kaggle.json`).
2. **Configure:** `cp .env.example .env`, then change `CARD_TOKEN_SECRET` and `API_KEY`. Optionally set `GEMINI_API_KEY` (free key from [Google AI Studio](https://aistudio.google.com/apikey)) for real AI explanations; without it a built-in fallback is used. The model is set by `LLM_MODEL` (default `gemini-3.5-flash`).
3. **Start and load everything:**
   ```bash
   make up        # Postgres, MongoDB, API (first build downloads ~2 GB of Python packages)
   make data      # download the dataset (~200 MB)
   make seed      # 300 cardholders + ~380k history payments (~2 min)
   make train     # train the fraud model (~40 s)
   make ingest    # embed the fraud-rules guide into pgvector
   ```
4. **Open http://localhost:8000**, paste your `API_KEY` into the field at the top, and press **Send 200 demo payments**. Click **Why?** on a declined payment.

Other commands: `make evaluate` (fraud + RAG evaluation), `make test`, `make lint`, `make loadtest`, `make demo`, `make down`.

## API

All responses are JSON. Errors look like `{"error": "..."}` and never include stack traces or card numbers.

| Method | Path | Auth | What it does |
|---|---|---|---|
| `POST` | `/api/payments` | `X-API-Key` + `Idempotency-Key` | Authorize a payment |
| `GET` | `/api/payments?status=DECLINED&page=1&page_size=50` | – | List API payments, newest first |
| `GET` | `/api/payments/{id}` | – | One payment with its fraud-check details |
| `GET` | `/api/payments/{id}/explain` | – | AI explanation of a declined payment |
| `GET` | `/api/stats` | – | Totals, approval rate, fraud-decline rate, payments over time |
| `GET` | `/health` | – | Status of both databases |
| `POST` | `/api/demo?count=200` | `X-API-Key`, dev only | Replay dataset payments |
| `GET` | `/api/dev/test-cards` | dev only | Test card numbers written by `make seed` |

Interactive docs: http://localhost:8000/docs

```bash
# Take a test card number from scripts/test_cards.json (created by `make seed`)
curl -X POST http://localhost:8000/api/payments \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: order-1001" \
  -H "Content-Type: application/json" \
  -d '{"card_number": "<test card>", "expiry_month": 12, "expiry_year": 2030,
       "amount": 24.99, "merchant_name": "Store 42", "merchant_category": "grocery_pos",
       "merchant_lat": 40.71, "merchant_lon": -74.0}'
# {"payment_id":"…","status":"APPROVED","response_code":"00","message":"Approved","risk_score":0}

# Sending the same Idempotency-Key again returns the same payment and charges nothing extra.

curl "http://localhost:8000/api/payments?status=DECLINED&page_size=5"
curl http://localhost:8000/api/payments/<payment_id>/explain
curl http://localhost:8000/api/stats
```

**Response codes:** `00` approved · `05` card blocked · `14` invalid card · `51` insufficient funds · `54` expired card · `59` suspected fraud.

## Why each tech choice

| Choice | Why |
|---|---|
| **PostgreSQL** for cards and payments | Money needs **ACID transactions**: a payment row and its balance change commit together or not at all. `SELECT … FOR UPDATE` row locks stop two simultaneous payments from overspending one card (tested). |
| **MongoDB** for fraud logs and AI explanations | Every fraud check produces different details (rules, reasons, a dozen features), so a **flexible schema** fits. The logs are append-only and grow fast, and a Mongo outage never blocks a payment. |
| **pgvector** inside Postgres | Vector search without running another database. 19 guide chunks don't need a dedicated vector store. |
| **FastAPI + Pydantic v2** | Typed request validation, automatic OpenAPI docs, async I/O. |
| **SQLAlchemy 2.0 async + Motor** | One request waiting on a database doesn't block the others. |
| **HistGradientBoostingClassifier** | Fast on 1.3M rows (training takes 35 s), handles missing values natively, and has class weighting for rare fraud. |
| **sentence-transformers (local)** | Free, private, offline embeddings. |
| **Gemini API (JSON mode) with a fallback** | Plain-English answers that cite their sources. JSON mode plus a response schema keeps the output machine-readable, and we still validate it. The template fallback means the feature still works without a key, when the API fails, or when a reply is blocked. |
| **Vanilla JS + Chart.js** | No build step and nothing to learn beyond the browser. |
| **Docker Compose** | One command starts the whole system the same way on any machine. |

## Dataset

[**Credit Card Transactions Fraud Detection Dataset**](https://www.kaggle.com/datasets/kartik2112/fraud-detection) by Kartik Shenoy on Kaggle, generated with the [Sparkov data generator](https://github.com/namebrandon/Sparkov_Data_Generation) by Brandon Harris. It has 1,296,675 training and 555,719 test transactions with a simulated `is_fraud` label. `fraudTest.csv` covers later dates than `fraudTrain.csv`, so the evaluation is time-based. The simulated names, addresses and birth dates are dropped when the files are loaded. The dataset isn't committed to this repository.

## Built with Claude Code

This project was built with [Claude Code](https://claude.com/claude-code), following a written phase-by-phase plan (setup → database → payment API → fraud checker → dashboard → RAG → tests/CI/load test → docs). Each phase ended with tests, a git commit and a report in [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md) that explains what was built and why. [`docs/PROJECT_EXPLAINED.md`](docs/PROJECT_EXPLAINED.md) walks through the whole system in plain language.

## Limitations and future ideas

**Limitations**
- **Simulated data.** Sparkov is widely used but generated. Real fraud is messier and changes over time.
- **The fixed 50/50 combination is weaker than ML alone here** (F1 0.656 vs 0.772). `too_fast` never fires on Sparkov, and `impossible_travel` mostly flags good payments, because the simulator scatters merchants around each cardholder's home.
- **No live traffic.** Demo payments use the current time, so time-based features differ from the dataset's.
- **The RAG check is small**: 10 questions written alongside the guide.
- **No `GEMINI_API_KEY` was set during the build**, so the measured explanations came from the fallback template. The Gemini path is unit-tested with a fake client.
- **Single machine.** The load test numbers are a lower bound.
- **Prepaid-style balances**: there's no separate authorization and settlement, and no refunds.

**Future ideas**
- Learn the rule/ML weights (or use ML for the decision and rules for the explanation), and tune the threshold by the cost of each error.
- Keep running per-card stats (count, mean, variance, last location) instead of re-reading the full history on every payment.
- Push dashboard updates with server-sent events instead of polling.
- Database migrations (Alembic), auth on the read endpoints, and rate limiting.
