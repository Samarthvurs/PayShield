# PayShield load test results

Command: `make loadtest` (Locust, headless). Each virtual user sends payments with a unique Idempotency-Key, using real fraudTest.csv transactions for seeded cards. A declined payment is a valid answer; only HTTP errors count as errors.

**Machine:** Host CPU: 12th Gen Intel Core i5-12450H (8 cores / 12 threads), 16 GB RAM, Windows 11; Docker VM: 12 vCPUs, 7.6 GB RAM; Linux 6.6.87.2-microsoft-standard-WSL2; API: Uvicorn (worker count in each run title); Postgres, MongoDB, API and Locust all on the same machine

## Summary

**Final setup (Run 3): 98.6 payments/sec, p50 150 ms, p95 390 ms, 0% errors** (30 users, 60 s).
That's **4.6× the baseline** (21.5/sec, p95 1,900 ms).

### The slowdown and the fix

Profiling one payment on the busiest card (3,126 history rows) showed about **50 ms of CPU work**:
loading the card's history (21 ms), building features with pandas (19 ms) and the model's
prediction (9 ms). The `(card_id, created_at)` index was already in place, so this wasn't a
missing-index problem. It's CPU work, and **one Uvicorn process can only use one CPU core**,
which caps throughput at about 20 payments/sec. That's exactly what Run 1 shows.

1. **Run 2: just add 4 workers.** This made it **10× worse** (2.3/sec). By default numpy and
   scikit-learn's OpenMP start one thread per core *in every process*, so 4 workers × 12
   threads fought over 12 cores, even to score one row.
2. **Run 3: 4 workers + one math thread each** (`OMP_NUM_THREADS=1` and friends, set only on
   the API server command in `docker-compose.yml`): **98.6/sec**.
3. **Run 4 isolates the thread setting:** with 1 worker it barely matters (21.5 → 23.5/sec).
   The gain comes from the two changes **together**.

The database pool was also reduced to 10 + 5 overflow connections per worker, so 4 workers
stay under Postgres's default limit of 100 connections.

The raw output of every run is kept below, in the order they ran.

## Raw runs

### Run 1 — baseline

_Run at 2026-10-24 09:48 UTC, 30 users for 60 s._

| Payments/sec | p50 latency | p95 latency | p99 latency | Requests | Error rate |
|---|---|---|---|---|---|
| 21.5 | 1200 ms | 1900 ms | 2600 ms | 1,256 | 0.00% |

### Run 2 — 4 Uvicorn workers

_Run at 2026-10-24 09:50 UTC, 30 users for 60 s._

| Payments/sec | p50 latency | p95 latency | p99 latency | Requests | Error rate |
|---|---|---|---|---|---|
| 2.3 | 9300 ms | 24000 ms | 27000 ms | 131 | 0.00% |

### Run 3 — 4 workers, 1 math thread each

_Run at 2026-10-24 09:52 UTC, 30 users for 60 s._

| Payments/sec | p50 latency | p95 latency | p99 latency | Requests | Error rate |
|---|---|---|---|---|---|
| 98.6 | 150 ms | 390 ms | 530 ms | 5,774 | 0.00% |

### Run 4 — 1 worker, 1 math thread (isolates the thread fix)

_Run at 2026-10-24 09:54 UTC, 30 users for 60 s._

| Payments/sec | p50 latency | p95 latency | p99 latency | Requests | Error rate |
|---|---|---|---|---|---|
| 23.5 | 1100 ms | 1700 ms | 2100 ms | 1,375 | 0.00% |
