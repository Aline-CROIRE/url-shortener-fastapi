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

## Phase 2 — Redis caching for redirects (cache-aside)

**Goal.** Most of the cache was built in Module 6 (F3.4a). This phase lines it up
with the Module 8 spec and adds the missing proofs. Cache-aside is like a sticky
note on your fridge: before walking to the shed (database) for the address book,
look at the fridge. Not there? Fetch it from the shed and write a sticky note for
next time. When the address changes, tear the note off.

**The flow of `GET /{short_code}`**
1. `GET url:redirect:{code}` in Redis. Found and not expired -> 302 at once.
2. Miss (or Redis down) -> read the database: 404 / 410 / live.
3. Live -> `SET url:redirect:{code} ... EX ttl NX`, then 302.
   TTL = 15 minutes, or less if the link expires sooner.
4. Update / switch off / delete -> after the commit, the key is replaced by a
   30-second tombstone (blocks a late write of the old value).

**What changed**

| File | Change |
|---|---|
| `app/cache/redirect_cache.py` | Key prefix `redirect:v1:` -> `url:redirect:` (spec naming) |
| `tests/test_redirect_cache_rules.py` | Key name; only redirect data stored; switched-off / deleted / expired links never cached; 302 on a hit; slow Redis = miss; Redis down never blocks an update or delete |
| `tests/test_redis_integration.py` | Cleans up keys with the new prefix |

Seen live: first visit (miss) 142 ms, second (hit) 32 ms.

**Why 302 and not 301.** 301 means "moved forever": the browser remembers it and
next time goes straight to the target without asking us. Those clicks never
reach our server, so analytics stop counting. 302 means "for now": the browser
asks us every time.

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| Cache-aside (app fills the cache on a miss) | Only links people visit are cached; Redis down = just slower | First visit is always a miss; invalidation is our job |
| Cache only `url_id`, `original_url`, `expires_at` | Small entries; changing the title never needs invalidation | Anything else needs the database |
| TTL capped at the link's expiry, plus an expiry check on read | An expired link is never served | Short-lived links are cached for less time |
| Tombstone + `SET NX` instead of a plain `DELETE` | A slow request cannot put the old target back | For 30 s after a change every visit reads the database |
| Fail open (Redis errors = miss) | Redirects never fail because of the cache | During a Redis outage every redirect hits the database |
| Short socket timeouts (0.5 s) | A hanging Redis cannot freeze redirects | A very slow but working Redis is treated as down |
| Rename the key prefix | Spec naming, easy to `SCAN` | Old `redirect:v1:` keys sit unused until their TTL ends (max 15 min) |

## Phase 3 — Async click tracking with Celery (write-behind)

**Goal.** A visit used to write two rows before the visitor got the 302 (INSERT
click + UPDATE click_count). Now the redirect only finds the target, answers, and
hands a small "ticket" to Celery; a worker writes the click a moment later. Like
a busy café: the cashier hands you your coffee at once and drops the receipt in a
box; someone files the receipts in the back. You never wait for the filing.

**The flow**
1. `GET /{code}` -> cache or DB lookup (read only) -> `Visit(original_url, click)`.
2. The route adds `clicks.track(click)` as a **background task**: it runs after
   the 302 is sent.
3. `CeleryClickTracker.track()` runs `track_click_task.delay(url_id, ip, user_agent,
   referrer, clicked_at)` in a thread (`.delay()` is blocking network I/O).
4. The worker runs `track_click_task`: one transaction with
   `INSERT INTO clicks ...` and `UPDATE urls SET click_count = click_count + 1`.

**What changed**

| File | Change |
|---|---|
| `app/services/clicks.py` | New: `ClickEvent` (plain values, `task_args()` / `from_task_args()`), `click_statements()` (the two writes, shared by worker and tests), `ClickTracker` protocol |
| `app/worker/tasks.py` | New: `track_click_task` with `autoretry_for`, `retry_backoff`, `retry_jitter`, `max_retries=5`; drops a click whose URL row is gone (foreign key error) |
| `app/worker/db.py` | New: the worker's SYNC engine (psycopg), created per worker process on its first task |
| `app/worker/tracker.py` | New: `CeleryClickTracker`, sends in a thread, logs and swallows every failure |
| `app/worker/celery_app.py` | Registers the tasks; publishing fails fast (no retry, 1 s connect timeout) |
| `app/services/redirect.py` | Reads only; returns `Visit` instead of writing the click |
| `app/api/routes/redirect.py` | Queues the click with `BackgroundTasks` after the response |
| `app/repositories/url.py` | `increment_click_count_statement()` (a statement, so sync and async can run it) |
| `app/db/errors.py` | `is_foreign_key_violation()` |
| `app/main.py`, `app/api/dependencies.py` | Tracker created in the lifespan, `ClickTrackerDep`; unused write-side click repository removed |
| tests | `test_click_task.py` (task directly + eager mode), `test_click_tracking.py`; conftest: `worker_db`, `celery_eager`, `recorded_clicks`, `InlineClickTracker` |

**Seen live (Docker):** miss 58 ms, hit 17 ms (Phase 2: 142 / 32 ms). Right after
3 visits `click_count` was 0; a moment later 3. With Redis stopped, redirects still
answered 302 from the database and logged "Click ... not queued"; the worker
reconnected by itself when Redis came back.

**Why only simple values in the task (never an ORM object)**
- The message travels as JSON through Redis; a `URL` object cannot be turned into JSON
  (and pickle, which could, can run code when read: we accept JSON only).
- An ORM object belongs to one session and one database connection in the API
  process. The worker is another process with its own connection.
- The object would be a stale copy: by the time the worker runs, the row may have changed.
  An id is always fresh: the worker asks the database.

**The race condition `click_count = click_count + 1` avoids**
Read-modify-write in Python: two workers both read 41, both write 42 -> one click lost.
`UPDATE urls SET click_count = click_count + 1` lets Postgres read and write under a
row lock in one step: the second waits for the first, reads 42, writes 43.

**Why the worker uses a sync session**
Celery calls a task as a normal function. Options were: (a) a separate sync engine
with psycopg, (b) `asyncio.run()` around the async code in each task. We chose (a):
Celery's normal style, and each worker process keeps a pool of reused connections.
(b) would start an event loop and open a new connection for every click. The cost of (a): the
same SQL is run by two drivers, so the statements live in one shared function.

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| Write-behind with Celery | Redirect never waits for writes; a DB hiccup does not slow visitors | `click_count` is a moment behind; clicks can be lost if the broker is down |
| Send after the response (`BackgroundTasks`) | The visitor never waits for Redis, even when it is slow | If the server process dies in that moment, the click is lost |
| `.delay()` in a thread | The event loop keeps serving others | Uses a thread from a limited pool (40) per click being sent |
| No publish retries, 1 s connect timeout | A dead broker costs ~1 s per click in the background, not 6 s | One failed try = one lost click |
| Broker down -> log and drop (no DB fallback) | Simple; the DB is not hit harder during an outage | Clicks during the outage are lost (logged) |
| `clicked_at` sent with the task | Clicks land on the right day even after retries | One more argument than the spec's four |
| Retry only DB connection/deadlock errors, with backoff + jitter | Survives a DB restart; retries spread out | A click can be saved up to ~2 minutes late |
| Drop a click whose URL row is gone | No endless retries | Such clicks are lost (logged) |
| Early ack (Celery default) | A click is never saved twice | If a worker crashes mid-task, that click is lost |
| Cache hit uses no database | Popular links work even while the DB is down | A row removed outside the API stays cached until its TTL (max 15 min) |
