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

## Phase 4 — Periodic task with Celery Beat (nightly archive)

**Goal.** Every night at 02:00 UTC, switch off (`is_active = false`) every live URL
whose expiry has passed, with ONE bulk UPDATE, then remove their cached
redirects. Like a library at closing time: one librarian walks the shelves once
with a list and pulls every overdue book in one round, instead of opening the
building once per book. Beat is the alarm clock that tells her when; the worker
does the walking.

**The flow**
1. `celery-beat` reads `beat_schedule` and, at 02:00 UTC, puts `urls.archive_expired` on the queue.
2. A worker runs `archive_expired_urls_task`:
   `UPDATE urls SET is_active = false, updated_at = now() WHERE is_active AND deleted_at IS NULL AND expires_at <= :now RETURNING short_code`
3. After the commit: `DELETE url:redirect:<code> ...` in batches of 500.
4. Logs `Archived N expired URLs`.

**What changed**

| File | Change |
|---|---|
| `app/repositories/url.py` | `archive_expired_statement(now)`: one bulk UPDATE ... RETURNING short_code |
| `app/worker/tasks.py` | `archive_expired_urls_task` (retries DB errors; safe to run twice) |
| `app/worker/cache.py` | New: the worker's sync Redis client + `clear_cached_redirects()` (batches, errors only logged) |
| `app/worker/celery_app.py` | `beat_schedule`: `crontab(hour=2, minute=0)` |
| `app/services/redirect.py` | 410 reason: expiry is checked before `is_active`, so an archived link still says `url_expired` |
| `tests/test_archive.py` | What is archived, one UPDATE, cache keys, Redis down, schedule, 410 reason, owner revives a link, the race with an owner (two real connections) |

**Seen live (Docker):** 1095 expired URLs (old seed data) archived in 0.5 s with one
UPDATE; the visited link then answered 410 `url_expired`.

**The race, and why the WHERE is in the UPDATE itself**
Owner: `BEGIN; UPDATE urls SET expires_at = <next week> WHERE id = 7;` (holds the row lock)
Archive: `UPDATE ... WHERE ... expires_at <= now` reaches row 7 and **waits**.
Owner: `COMMIT`. Postgres then **re-checks the archive's WHERE on the new row**
(READ COMMITTED): next week is not `<= now`, so row 7 is skipped. If we had first
SELECTed the ids and then updated "WHERE id IN (...)", the old snapshot would win
and the owner's fresh extension would be switched off. A test proves both ways
(we broke the code on purpose and the test failed).

**Behaviour changes (explained)**
- A link that is switched off AND expired now answers `url_expired` (was `url_inactive`).
- An archived link stays off if the owner only extends `expires_at`; they send
  `{"is_active": true, "expires_at": ...}` (the Free limit is checked again then).
- Archiving sets `updated_at`: it is a real change of the link (unlike a click).

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| One bulk UPDATE (not one per URL) | 1 round trip for 1095 rows (0.5 s) | All matching rows are locked until the commit; a huge backlog = one long transaction |
| `RETURNING short_code` | Codes for the cache cleanup without a second query | Holds every code in memory (fine for thousands) |
| WHERE inside the UPDATE (no SELECT first) | Postgres re-checks rows changed meanwhile: no lost extension | — |
| Same "expired" rule as the redirect (`<=`) | Visitors and the archive never disagree | Spec text said `<`; the difference is one instant |
| Clear cache after commit; errors only logged | The DB change always stands | If Redis is down, entries live until their TTL (already capped at the expiry) |
| DELETE in batches of 500 | Redis is never blocked by one giant command | A few more round trips |
| Nightly at 02:00 UTC with `crontab` | Quiet hour; one run a day | A link can stay "on but expired" up to a day (visitors still get 410) |
| Exactly one beat container | Each job queued once | If beat is down, the night's run is skipped (the next one catches up) |
| Task safe to run twice | Retries and manual runs are harmless | — |

## Phase 5 — Structured JSON logging

**Goal.** Every log line becomes one JSON object with named fields, and every line
written while a request runs carries that request's id. Like parcels in a sorting
centre: a handwritten note ("something about a 401, around 9") is hard to sort; a
label with fixed boxes (date, sender, tracking number) can be sorted by machine,
and the tracking number links every scan of the same parcel.

**One real line (seen live in Docker)**
```json
{"timestamp": "2026-11-27T09:07:56.782Z", "level": "WARNING", "logger": "app.security",
 "message": "Failed login for n***@example.com from ip=172.20.0.1",
 "request_id": "live-check-42", "path": "/api/v1/auth/login", "method": "POST"}
```
The access line of the same request adds `"status": 401, "duration_ms": 369.15`.

**How the request id reaches a line written deep in a service**
1. `RequestLoggingMiddleware` takes the caller's `X-Request-ID` (if plain) or makes one,
   and calls `bind_request_context(id, method, path)`: three **ContextVars**.
2. A ContextVar has one value per request (per asyncio task), so two requests at
   the same moment never mix their ids.
3. `RequestContextFilter` (on the handler) copies them onto every LogRecord.
4. `JsonFormatter` writes the record as one JSON line; the stack trace (if any)
   goes in `"exception"`. Both are passed through `redact()` first.

**What changed**

| File | Change |
|---|---|
| `app/core/logging.py` | ContextVars + `bind_request_context`, `RequestContextFilter`, `JsonFormatter`, `TextFormatter`, `redact()`, `logging_config()` for `dictConfig`, `setup_logging()` once per process |
| `app/core/middleware.py` | Binds the request context; access line with `status` and `duration_ms` as fields |
| `app/core/config.py`, `.env.example` | `LOG_FORMAT` = `json` (default) or `text` |
| `app/main.py` | Passes `log_format` |
| `app/worker/celery_app.py` | `setup_logging` signal: worker and beat log the same JSON |
| `tests/test_json_logging.py` | Format, fields, request id on inner lines, 500 with stack trace, redaction, a full login session, dictConfig, Uvicorn/Gunicorn/Celery loggers |

Already there from Module 7 (and still tested): failed logins (email masked, never
the password), every 403, rate-limit hits, 500s with the stack trace (never sent to
the client), log-injection cleaning of client text.

**Never log secrets: three layers**
1. Our code never passes a password or token to a logger (masked email, user id only).
2. `redact()` replaces JWTs (`eyJ...`), `Bearer ...`, `password=...`, `refresh_token=...`
   in every message and stack trace, as a safety net for library messages.
3. A test runs a whole session (register, wrong password, login, /me, refresh,
   logout) and checks the password, both tokens and the full email are nowhere in the output.

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| JSON lines | Collectors filter by field (`status >= 500`, one `request_id`) | Harder to read by eye (`LOG_FORMAT=text` for that) |
| Our own ~40-line formatter | No new library; every line explainable | We maintain it (python-json-logger would do the same) |
| stdout only (no log files) | Docker/Kubernetes collect it; no disk filling up, no rotation | Without a collector, old logs are gone when the container is removed |
| ContextVars for the request id | Every line of a request is linked, with no parameter passing | Lines from threads started by hand do not inherit them (asyncio tasks do) |
| Redaction by patterns | Catches secrets in library messages too | Small cost per line; an unusual secret format could slip through |
| `setup_logging` once per process | A second `create_app()` (tests, scripts) cannot remove handlers | The first call decides (tests force a re-setup when they test it) |
| Uvicorn/Gunicorn access logs off | One access line per request, with our fields | Their own format is no longer available |
| Celery uses the same setup | One format for API, worker and beat | Worker lines have no request id (a task is not a request) |

## Phase 6 — Health probes and the production server

**Goal.** Tell the outside world two different things: "the process is alive" and
"it can serve requests right now", and run the app under Gunicorn with a worker
count whose database connections are guaranteed to fit. Like a shop: the lights
being on (alive) is not the same as the till working and the shelves stocked
(ready). If the delivery truck is late (database down), you put up "back soon"
(not ready); you do not knock the shop down and rebuild it (restart).

**Liveness vs readiness**

| Probe | Question | Checks | On failure the orchestrator... |
|---|---|---|---|
| `/health/live` | Is the process answering? | nothing | restarts the container |
| `/health/ready` (= `/health`) | Can it serve real requests? | `SELECT 1`, Redis `PING`, replica if set | stops sending traffic (no restart) |

Why split: if liveness checked the database, a database outage would make every
app container fail liveness and restart, again and again (a "restart storm"),
which fixes nothing. Docker's `HEALTHCHECK` now uses `/health/live`.
Each check gives up after 2 s (`asyncio.timeout`) so the probe always answers
before Docker's 5 s timeout; responses have `Cache-Control: no-store`.

**Seen live (Gunicorn on port 8001):**
`live 200 / ready 200` -> Redis stopped -> `live 200 / ready 503 (13 ms)` -> Redis back -> `ready 200`.

**Gunicorn (`gunicorn.conf.py`)**
- `worker_class = "uvicorn_worker.UvicornWorker"`: Gunicorn manages processes, Uvicorn speaks ASGI.
- `workers = WEB_CONCURRENCY or 2 x CPU + 1`.
- `timeout 30` (kill a stuck worker), `graceful_timeout 30` (finish requests on deploy), `keepalive 5`.
- `max_requests 1000 + jitter 100`: workers are replaced now and then (memory leaks cannot grow forever).
- `logconfig_dict` = our JSON config: Gunicorn's own lines are JSON too; its access log is off.

**The connection math (`app/core/capacity.py`)**
Every worker process has its OWN pool. At the busiest moment:
`web workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW) + Celery processes x 2 + 10 reserved <= max_connections`
- Live: `3 x (3 + 2) + 2 x 2 = 19` -> "Gunicorn ready: 3 workers, up to 19 database connections".
- `WEB_CONCURRENCY=40`: `40 x 5 + 4 = 204 > 90` -> Gunicorn refuses to start, with that sentence.
- Without the check, PostgreSQL would answer "too many clients already" at the busiest moment.
- Pool per worker is small on purpose (3 + 2): an async worker serves many requests on few connections.

**What changed**

| File | Change |
|---|---|
| `app/api/routes/health.py` | `/health/live`; `/health` = `/health/ready`; Redis down -> 503; 2 s timeouts; `no-store` |
| `app/core/capacity.py` | New: `default_web_workers`, `connections_needed`, `check_connection_budget` |
| `app/core/config.py` | `WORKERS` -> `WEB_CONCURRENCY`; `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_MAX_CONNECTIONS`, `CELERY_CONCURRENCY` |
| `app/db/database.py`, `app/main.py` | Pool sizes passed to both engines |
| `gunicorn.conf.py` | Worker count, budget check, `max_requests`, JSON `logconfig_dict`, `when_ready` log |
| `Dockerfile` | `HEALTHCHECK` on `/health/live` |
| `docker-compose.prod.yml`, `.env.example` | New sizing variables |
| tests | `test_health_probes.py`, `test_gunicorn_config.py`; Redis-down health is now 503 |

**Behaviour changes (explained)**
- `/health` answers 503 when Redis is down (Module 6: 200). Redis now also carries the click queue.
- `WORKERS` is renamed `WEB_CONCURRENCY` (the standard name).

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| Separate live and ready probes | No restart storms during a DB outage | Two endpoints to understand and configure |
| Redis counts for readiness | Traffic stops when clicks would be lost | Redirects (which work without Redis) also get no traffic from a strict balancer |
| 2 s timeout per check | The probe always answers | A database slower than 2 s counts as down |
| `2 x CPU + 1` workers by default | Uses every core | On big machines it needs the budget check / a small pool |
| Refuse to start over budget | A wrong setting fails at deploy, not at peak traffic | Must know `max_connections` (`DB_MAX_CONNECTIONS`) |
| Small pool per worker (3 + 2) | Many workers fit in 100 connections | Under heavy load a request may wait for a free connection (up to 10 s) |
| `max_requests` recycling | Memory leaks stay bounded | A worker restart now and then (graceful, jittered) |
| A PgBouncer in front of Postgres (not done) | Thousands of client connections on few server ones | One more service to run |
