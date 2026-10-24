# PayShield, explained

This is written for you, the person presenting this project. It explains every part in plain language, so you can answer any question about it in an interview. All numbers are real outputs from the scripts in this repo.

---

## 1. One payment, start to finish

Imagine a shop sends this to PayShield: *"Charge $392 to card 4…1234, online shopping, from this location."*

1. **The request arrives** at `POST /api/payments` with two headers: `X-API-Key` (proves the caller is allowed) and `Idempotency-Key` (a unique id for this payment attempt).
2. **API key check.** A wrong key gets `401` immediately.
3. **Idempotency check.** If we've seen this `Idempotency-Key` before, we return the saved answer. If the shop's network dropped and it retried, the customer still pays only once.
4. **Luhn check.** A quick checksum catches typos in the card number → decline `14`.
5. **Tokenize and look up the card.** We never store card numbers. We compute `HMAC-SHA256(secret, card number)` and look up that token. Unknown card or wrong expiry → `14`. Blocked → `05`. Expired → `54`.
6. **Lock the card row** (`SELECT … FOR UPDATE`). If a second payment for the same card arrives right now, it waits here.
7. **Balance check.** Not enough money → `51`.
8. **Fraud check.** We load the card's earlier payments, compute features (how big is this amount compared with usual? how many payments in the last minute/hour/day? how far from the last payment, and how fast would you have had to travel?), run 3 rules and the ML model, and combine them into a risk score. Score ≥ 48.5 → `59` "Suspected fraud".
9. **Approve.** Deduct the balance, save the payment and **commit**: the payment row and the balance change happen together or not at all.
10. **Log the fraud check to MongoDB** (scores, rules, reasons, features, last 4 digits only).
11. **Respond:** `{"status": "DECLINED", "response_code": "59", "risk_score": 67, ...}`.
12. Later, someone on the dashboard clicks **"Why?"**. PayShield finds the 3 most relevant passages of the fraud-rules guide and asks Google Gemini to explain the decline using only those passages. The answer is cached in MongoDB.

---

## 2. The four parts

### Part 1 — Payment API (`app/main.py`, `app/payments.py`)
- **What:** a FastAPI web service that authorizes payments and serves data to the dashboard.
- **How:** the flow in section 1. Every check maps to a standard response code (`00, 05, 14, 51, 54, 59`), similar to what real card networks use.
- **Why built this way:**
  - *Idempotency keys*, because networks fail and clients retry. Charging twice is the worst bug a payment system can have. Even two simultaneous retries with the same key create only one payment: the database's `UNIQUE` constraint lets one insert win, and the other request returns the winner's answer.
  - *Row locking*, because two payments at the same moment could both see "$100 available" and both spend $60. With `FOR UPDATE` the second one waits and then sees $40 (tested: exactly one `00` and one `51`).
  - *Clean errors.* FastAPI's default validation error repeats the input back, which would leak the card number, so we replaced it.

### Part 2 — Fraud checker (`app/fraud/`)
- **What:** 3 explainable rules + a gradient boosting model → one score from 0 to 100.
- **How:** see section 4.
- **Why:** rules are easy to explain and need no data. The model learns patterns from 1.3 million transactions. Combining them gives one decision, plus reasons a human can read.

### Part 3 — Dashboard (`frontend/`)
- **What:** one HTML page: stat cards, a chart, the latest 50 payments (auto-refreshing every 5 s), a test-payment form, a demo button and a "Why?" panel.
- **How:** plain JavaScript calls the JSON API with `fetch`. Everything is inserted with `textContent`, so a malicious merchant name can't run code (that's how XSS is prevented).
- **Why:** no framework means no build step and nothing hidden, and it works on phones. The API key is never in the code: you paste it into the page and it stays in your browser.

### Part 4 — AI explainer (`app/ai/`)
- **What:** "Why was this declined?" in plain English, citing the guide.
- **How:** see section 5.
- **Why:** customers and support agents need reasons, not just a code. Grounding the answer in our own guide stops the AI from inventing rules.

---

## 3. The databases

### PostgreSQL (with pgvector)
| Table | What's in it | Why Postgres |
|---|---|---|
| `cards` | token, last 4 digits, expiry, status (active/blocked), balance, home location | Balances are money. They need transactions and row locks. |
| `payments` | card, amount, merchant, location, status, response code, risk score, idempotency key (unique), dataset label (seeded history only), `source` (seed/api), time | Must commit **together** with the balance change (ACID). The unique key enforces idempotency. The `(card_id, created_at)` index makes "this card's history" fast. |
| `guide_chunks` | source file, chunk number, text, 384-number embedding | pgvector adds a `vector` type and a cosine-distance operator, so there's no separate vector database to run. |

### MongoDB
| Collection | What's in it | Why Mongo |
|---|---|---|
| `fraud_checks` | payment id, last 4, rule/ML/final scores, threshold, rules triggered, reasons, all features, decision | The shape varies per check (different rules fire, features may be missing). It's append-only and grows with every payment. It's written **after** the Postgres commit, so if Mongo is down the payment still succeeds (tested). |
| `ai_explanations` | payment id, summary, reasons, sources, model | A cache of LLM output: a flexible document, written once and read many times. |

**One-line answer:** *"Postgres holds the money because it needs ACID transactions and locks; MongoDB holds the logs because they're flexible, append-only documents that must never slow down or block a payment."*

---

## 4. The fraud checker

### Features (`app/fraud/features.py`)
For each payment, using **only earlier payments** of the same card:

| Feature | Meaning |
|---|---|
| `amount` | the payment amount |
| `amount_zscore` | how many standard deviations above the card's average this amount is |
| `txn_count_60s / 1h / 24h` | how many payments the card made in the last minute / hour / day |
| `secs_since_prev` | time since the card's previous payment |
| `distance_km`, `speed_kmh` | distance from the previous payment's location, and the speed you'd need to travel it |
| `hour` | hour of day |
| `category_code` | merchant category (14 types) |

**The same function** computes these for training (1.3M rows at once) and for a live payment (the card's history plus one new row). That prevents *training/serving skew*, and a test checks the two give identical numbers.

### The 3 rules (`app/fraud/rules.py`)
| Rule | Fires when | Points |
|---|---|---|
| `too_fast` | more than 5 payments in 60 s | 35 |
| `unusual_amount` | amount > 3 standard deviations above usual (needs 5+ past payments) | 30 |
| `impossible_travel` | travel faster than 900 km/h since the last payment | 35 |

`rule_score = min(100, sum of points)`.

### The ML model
A **HistGradientBoostingClassifier**: hundreds of small decision trees, each one trained to fix the mistakes of the trees before it. It was trained on the first 90% of `fraudTrain.csv` by date (1,167,007 rows), with **class weighting** because only 0.57% of rows are fraud. `ml_score` = fraud probability × 100.

### The combined decision
`final = 0.5 × rule_score + 0.5 × ml_score`, and we decline if `final ≥ 48.5`. The threshold 48.5 was chosen on the **validation set** (the last 10% of training dates, 129,668 rows) as the value with the best F1. It was not chosen on the test set, which would be cheating.

### What the evaluation numbers mean (`docs/evaluation.md`)
Test set: `fraudTest.csv`, **555,719 payments from later months, 2,145 fraud (0.386%)**.

| Approach | Precision | Recall | F1 | PR-AUC | FPR |
|---|---|---|---|---|---|
| Rules only | 0.047 | 0.467 | 0.085 | 0.038 | 3.68% |
| ML only | 0.781 | 0.765 | 0.772 | 0.848 | 0.083% |
| Combined | 0.538 | 0.839 | 0.656 | 0.611 | 0.279% |

- **Precision 0.781 (ML only):** when the model says "fraud", it's right 78% of the time.
- **Recall 0.839 (combined):** the combined checker catches 84% of all fraud (1,799 of 2,145).
- **False positive rate 0.279% (combined):** about 3 in every 1,000 good payments are wrongly declined (1,542 people annoyed).
- **Why not accuracy?** Approving everything would be 99.6% "accurate" and catch zero fraud.
- **PR-AUC** summarises precision vs recall across all thresholds. Random guessing would score about 0.004 here (the base rate), and the model gets 0.848.
- **The honest finding:** the fixed 50/50 combination catches more fraud than ML alone (recall 0.84 vs 0.77) but wrongly declines 3× more good customers, so its F1 is lower. Why? On this simulated data `too_fast` never fires, and `impossible_travel` fires 14,176 times but only 190 are fraud, because the simulator scatters merchants around each cardholder's home. `unusual_amount` is the useful rule (12% precision, about 30× the base rate). **In an interview, say this openly:** it shows you measure instead of assuming. The fix would be learning the weights, or using ML for the decision and rules for the explanation.

---

## 5. The RAG explainer, step by step

**RAG = Retrieval-Augmented Generation:** first *retrieve* relevant text, then let the LLM *generate* an answer from it.

1. **Write the knowledge** (`knowledge_base/*.md`): a fictional fraud-rules guide, decline codes and fraud patterns.
2. **Chunk** it by heading (≤ 300 words each) → 19 chunks.
3. **Embed** each chunk with `all-MiniLM-L6-v2`. That turns text into 384 numbers, so texts with similar meaning get similar vectors. It runs locally and costs nothing.
4. **Store** chunks and vectors in Postgres (`guide_chunks`, pgvector).
5. **When "Why?" is clicked:** build a query from the decline ("code 59, too_fast, 6 payments in 60 seconds…"), embed it, and ask Postgres for the 3 nearest chunks by cosine distance (`ORDER BY embedding <=> query LIMIT 3`).
6. **Prompt Gemini** with only *safe facts* (last 4 digits, amount, category, scores, rules, reasons; never the card number, which a test checks) and the 3 excerpts. The instruction: answer only from these, reply as JSON `{summary, reasons, sources}`. Gemini's **JSON mode** (`response_mime_type="application/json"` plus a response schema) makes it return JSON directly.
7. **Validate** the JSON with Pydantic and drop any source id that wasn't given. If the JSON is invalid, ask once more.
8. **Fallback:** with no API key, an API error, or a reply blocked by Gemini's safety filters, a template builds the explanation from the rule reasons and the top chunk.
9. **Cache** the result in MongoDB.

**Why RAG instead of just asking the LLM?**
- The LLM doesn't know *our* rules and thresholds. Without them it would guess, which is called hallucinating.
- Every answer cites the passages it used, so you can check it.
- Updating the rules means editing a Markdown file and re-running `make ingest`, with no retraining.

**Measured:** retrieval hit rate @3 = **100%** (10 of 10 test questions found the right file). Honest caveat: 10 questions written alongside the guide is an easy test.

---

## 6. Glossary

| Term | Plain meaning |
|---|---|
| **Idempotency** | Doing the same request twice has the same effect as doing it once. Here, a retry never charges twice. |
| **ACID** | Database transactions are Atomic (all or nothing), Consistent, Isolated (they don't see each other's half-done work) and Durable (saved even after a crash). |
| **Row locking** | `SELECT … FOR UPDATE` reserves a row, so other transactions that want it wait until you commit. |
| **Tokenization** | Replacing a card number with a meaningless token. Ours is HMAC-SHA256 with a secret, so it can't be reversed without the secret. |
| **Luhn** | A checksum on card numbers that catches most typing errors. |
| **Response codes** | Two-digit answers like `00` approved, `51` insufficient funds, `59` suspected fraud. |
| **z-score** | (value − average) ÷ standard deviation: "how unusual is this number?" 3 or more is very unusual. |
| **Gradient boosting** | Many small decision trees, each correcting the previous ones' errors. |
| **Precision / recall** | Precision: of the payments we flagged, how many were fraud? Recall: of all the fraud, how much did we catch? |
| **F1** | One number balancing precision and recall (their harmonic mean). |
| **PR-AUC** | Area under the precision–recall curve: quality across every threshold. Best for rare events. |
| **Embeddings** | Text turned into a list of numbers, where similar meanings give nearby vectors. |
| **RAG** | Retrieve relevant documents, then have the LLM answer from them. |
| **p95 latency** | 95% of requests finished faster than this. It shows the slow tail that an average hides. |
| **CI/CD** | Continuous Integration (every push is automatically linted and tested) / Continuous Delivery (automatically ready to release). |

---

## 7. Visa JD requirements → PayShield

| JD requirement | Where PayShield shows it |
|---|---|
| Python, JavaScript, HTML | FastAPI backend and fraud checker in Python; dashboard in HTML/CSS/JavaScript |
| OOP, data structures, algorithms | Typed modules and dataclasses; the Luhn algorithm; sliding time windows with binary search (`np.searchsorted`) over 1.85M rows |
| Software design / SDLC | Phased build: design → code → test → CI → docs, with one commit and one report per phase |
| CI/CD, automated testing | GitHub Actions with real Postgres and Mongo; 49 pytest tests; 96.1% coverage |
| Secure development | Card tokenization; no card number stored, logged, echoed in errors or sent to the AI (all tested); constant-time API key check; XSS-safe UI |
| Reliability & scalability | Idempotency, row locking, a Mongo outage doesn't block payments, load test 98.6 payments/sec at p95 390 ms |
| Web UI design | Responsive dashboard with loading, empty and error states |
| PostgreSQL | Cards, payments, transactions, indexes, pgvector |
| MongoDB | Fraud-check logs and AI explanation cache |
| AI/ML concepts | Gradient boosting, class imbalance, time-based validation, precision/recall/PR-AUC on 555k test rows |
| Prompt engineering, RAG, LLMs | Grounded prompt, JSON output validation, retry, fallback, citations |
| AI coding tools | Built with Claude Code |
| Customer focus | Plain-English reasons for every decline, with what to do next |

---

## 8. Ten likely interview questions

1. **"How do you stop a customer being charged twice?"** Idempotency keys. The client sends a unique key per payment. If we've seen it, we return the saved response. The key has a `UNIQUE` constraint, so even two simultaneous retries create only one payment: the loser gets an integrity error and returns the winner's response.
2. **"Two payments for the same card at the same millisecond?"** The card row is locked with `SELECT … FOR UPDATE` inside the transaction. The second waits, then sees the updated balance. There's a test with $100 and two $60 payments: exactly one succeeds.
3. **"Why two databases?"** Money needs ACID transactions and locks, so that's Postgres. Fraud logs are flexible, append-only documents that must never block a payment, so that's Mongo (written after commit; an outage is tested).
4. **"How do you protect card numbers?"** We store only an HMAC-SHA256 token and the last 4 digits. The number never appears in the DB, logs, validation errors or AI prompts, and there's a test for each.
5. **"How did you evaluate the model?"** Time-based: trained on earlier months, tested on 555k later transactions, with the threshold chosen on a separate validation slice. Reported precision, recall, F1, PR-AUC and FPR. Not accuracy, because fraud is 0.4%.
6. **"Your combined score is worse than ML alone. Why keep it?"** The F1 is lower but the recall is higher: it catches 159 more frauds at the cost of more false declines. That's a business trade-off. Next step: learn the weights or use ML to decide and rules to explain. I found this by measuring, not assuming.
7. **"What's training/serving skew and how did you avoid it?"** When the model sees different inputs in production than in training. One feature function serves both, and a test checks they match.
8. **"Why RAG and not just ask Gemini or ChatGPT directly?"** The model doesn't know our rules. RAG gives it our guide, it must answer from it, cite it, and invalid citations are dropped. Rules change by editing a document.
9. **"How did you find and fix a performance problem?"** Load test: 21.5/sec. Profiling showed about 50 ms of CPU per payment (history, pandas, model), and one process uses one core. Adding 4 workers alone made it 10× *worse* because of thread oversubscription. 4 workers with one math thread each gave 98.6/sec and p95 390 ms. Measured before and after.
10. **"What would you change for production?"** Pre-aggregated per-card stats instead of reading full history; learned weights and a cost-based threshold; auth on reads and rate limiting; migrations; separating authorization from settlement; real traffic monitoring for model drift.

---

## 9. Resume bullets

- Built **PayShield**, a real-time payment authorization API (Python, FastAPI, PostgreSQL, MongoDB) with idempotency keys and row-level locking that prevent double charges and overspending under concurrent requests; **49 automated tests, 96.1% coverage**, CI on GitHub Actions.
- Trained a gradient boosting fraud model on **1.3M card transactions** and evaluated it on **555K later, unseen transactions**: **precision 0.78, recall 0.77, PR-AUC 0.85** at a **0.08% false positive rate**, and compared it against rule-based and hybrid approaches.
- Added a **RAG explainer** (pgvector + Google Gemini API) that turns declines into cited, plain-English reasons (**100% retrieval hit rate @3**), and load-tested the API to **98.6 payments/sec at p95 390 ms** after diagnosing a CPU thread-oversubscription bottleneck (**4.6× speedup**).

---

## 10. Two-minute demo script

1. **(15 s) The problem.** "Every card payment needs a yes or no in milliseconds, and the no has to be explainable. PayShield is a payment API that does that."
2. **(20 s) Architecture.** Show the README diagram: API → fraud check (3 rules + ML) → Postgres for money, Mongo for logs, RAG explainer on top.
3. **(25 s) Live payments.** Open `localhost:8000`, click **Send 200 demo payments**. Point out the stat cards, the chart and the green/red table filling up. "These are real transactions from a public fraud dataset."
4. **(20 s) Edge cases.** Use the test form with the *Blocked card* → `05`, then the *Low balance card* with $50 → `51`. "Every decline has a standard response code."
5. **(25 s) Explainability.** Filter **Declined** and click **Why?** on a `59`. Read the summary and reasons, and point at the source citations. "The AI can only use our fraud guide, so it can't invent rules."
6. **(15 s) Results.** "On 555,000 unseen transactions the model has 78% precision and 77% recall at a 0.08% false positive rate, and the API handles about 100 payments a second."
7. **(10 s) Engineering.** "Idempotency and row locking are tested against concurrent requests, the card number is never stored, 49 tests run in CI, and I built it phase by phase with Claude Code."
