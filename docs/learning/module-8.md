# Module 8 — Performance & Production Readiness (notes)

Short notes written while building. The full teach-back (key code line by line,
keywords, check-yourself questions) is added after the lab.

## Phase 1 — Infrastructure

**Goal.** Give the app a "back office": a job queue (Celery, with Redis as the
broker), a worker that runs the jobs, and beat, a clock that puts scheduled jobs
on the queue. Like a restaurant: the waiter (API) writes the order on a ticket
(job) and pins it on the rail (broker); the cook (worker) takes it from there,
and the kitchen timer (beat) adds the nightly cleaning ticket.

**What changed**

| File | Change |
|---|---|
| `app/core/config.py` | `CELERY_BROKER_URL` (default Redis DB 1); refused if it is not Redis or is the cache's DB; `sync_database_url` (psycopg driver for the worker) |
| `app/worker/celery_app.py` | The Celery app: JSON only, no stored results, UTC |
| `docker-compose.yml` / `.prod.yml` | `celery-worker` and `celery-beat` services with healthchecks and `depends_on: service_healthy`; one shared settings block (YAML anchor); prod Redis `volatile-lru` |
| `.env.example` | `CELERY_BROKER_URL`, comments on DB 0 / DB 1 |
| `requirements.txt` | `celery[redis]`, `psycopg[binary]` and their pinned dependencies |
| `tests/test_infrastructure.py` | Settings, Celery config, compose files read as YAML |

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| Redis as broker (not RabbitMQ) | Already running for the cache; one less service | Fewer delivery guarantees than RabbitMQ; a Redis crash can lose queued jobs |
| Cache DB 0, broker DB 1 | Clearing the cache never deletes jobs; easy to inspect each | Same server: one memory limit and one eviction policy for both |
| Prod Redis `volatile-lru` | When memory is full only cache keys (they have a TTL) are evicted | If memory is full of jobs, Redis refuses new writes instead of evicting |
| JSON serializer only | A message from the queue can never run code (pickle could) | Only simple values in task arguments (which we want anyway) |
| No result backend | No extra Redis writes nobody reads | Cannot ask "did task X finish?" (logs answer that) |
| Worker uses sync psycopg | Celery's normal style; a pool of reused connections | The small click-writing code exists in an async and a sync form |
| Beat files in `/tmp` | Works with the non-root image user | The schedule state is lost on container restart (it is rebuilt from code) |
| Worker healthcheck = `inspect ping` | Proves the worker really reads from the broker | Each check starts a small Python process (every 30 s) |
