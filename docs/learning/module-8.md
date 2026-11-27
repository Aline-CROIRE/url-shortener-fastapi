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

## Phase 7 — Documentation and final check

**What changed**
- `README.md`: Celery in the quick start (Docker and local, `--pool=solo` on Windows),
  useful Docker commands, every new setting, the new architecture diagram (client ->
  API -> Redis / PostgreSQL, API -> broker -> worker, beat -> worker), the new redirect
  flow (cache-aside + write-behind), the three health endpoints, "Module 8 design
  decisions", and the new known limits.

**Final check (2026-11-27)**
- Clean clone (only tracked files, no `.env`, new volumes, project `m8clean`):
  `docker compose up --build` -> db, redis healthy; migrate exited 0; app, celery-worker,
  celery-beat healthy. Register -> login -> create -> 2 redirects -> `click_count` 2 (saved
  by the worker). `/health/ready` 200.
- Tests: 919 passed, coverage 100%. `ruff check`, `ruff format --check`, `mypy`: clean.

## Review quiz (10 questions)

Answer in your own words first, then open the answers.

1. Walk through `GET /{short_code}` on a cache hit and on a cache miss. Which steps touch
   Redis, PostgreSQL and the broker, and in what order?
2. Why is a redirect cached for at most 15 minutes AND never past the link's `expires_at`?
   What would go wrong with only one of the two rules?
3. When the owner updates a link, why does the cache get a 30-second tombstone with
   `SET NX` instead of a plain `DELETE`? Tell the story of the race in three steps.
4. Why must `track_click_task` receive ids and strings and never a `URL` object?
5. Two clicks on the same link arrive at the same moment. Show why
   `click_count = click_count + 1` in SQL counts both, while "read, add 1, write" loses one.
6. Which errors does the click task retry, which does it drop, and which does it raise? Why
   are backoff and jitter used?
7. The broker is down. What happens to the visitor, to the click, to the logs and to
   `/health/ready`? Why did we choose not to fall back to writing the click in the request?
8. Why does the nightly archive put its condition inside one `UPDATE ... WHERE ...` instead
   of `SELECT`ing the ids first and updating them afterwards?
9. What is the difference between liveness and readiness? Which one does Docker's
   `HEALTHCHECK` use here, and what would go wrong with the other one?
10. With `WEB_CONCURRENCY=8`, `DB_POOL_SIZE=3`, `DB_MAX_OVERFLOW=2` and 2 Celery processes,
    how many database connections can the app open? Does it fit in `max_connections=100`
    with 10 reserved? What does Gunicorn do if it does not?

<details>
<summary>Answers</summary>

1. **Hit:** Redis `GET url:redirect:{code}` -> entry found and not expired -> 302; no
   database. **Miss:** Redis GET finds nothing -> `SELECT` the URL in PostgreSQL -> 404 /
   410 / live -> `SET url:redirect:{code} ... EX ttl NX` -> 302. **Both:** after the
   response, a background task calls `track_click_task.delay(...)` (Redis DB 1, the
   broker); a worker then writes the click to PostgreSQL.
2. 15 minutes bounds how long a changed link could be stale if an invalidation is ever
   missed (e.g. Redis was down during the update). The expiry cap makes sure an expired
   link is never served from the cache. With only 15 min, a link expiring in 2 minutes
   would redirect 13 minutes too long; with only the expiry, a link without an expiry
   could stay cached (and stale) forever. (The read path also checks the expiry.)
3. (1) A visitor misses the cache and reads the link from the database (still live).
   (2) The owner deletes it, commits, and invalidates the cache. (3) The visitor now
   writes what it read in step 1 into the cache: the deleted link would redirect for 15
   minutes. With a tombstone + `SET NX`, step 3 finds the key already taken and is refused.
4. The message travels as JSON through Redis (an ORM object cannot be JSON; pickle could
   run code, so we accept JSON only). The object belongs to a session and connection in
   the API process; the worker is another process. And it would be a stale copy: by the
   time the worker runs, the row may have changed. An id lets the worker read fresh data.
5. Read-modify-write: A reads 41, B reads 41, A writes 42, B writes 42 -> one click lost.
   In SQL, PostgreSQL locks the row for the UPDATE: B waits for A's commit, then computes
   42 + 1 = 43. Both count.
6. Retried: `OperationalError` / `InterfaceError` (database restarting, connection lost,
   deadlock), up to 5 times, waiting 1, 2, 4, 8, 16 s (backoff) plus randomness (jitter) so
   that after an outage the retries do not all hit the database at the same moment.
   Dropped: a foreign-key error (the URL row is gone; retrying cannot help), logged.
   Raised: anything else (a bug; retrying would repeat it).
7. The visitor still gets the 302 at once (the click is queued after the response). The
   one try to publish fails within ~1 s; the click is lost and a WARNING "Click for
   url_id=... not queued" is logged with the stack trace. `/health/ready` answers 503 (Redis
   is down), so a load balancer can stop sending traffic. No fallback to a direct database
   write: during an outage it would put the database back into every redirect, the exact
   load we moved away, and the code would have two write paths.
8. With the condition in the UPDATE, PostgreSQL checks it while it locks each row. If the
   owner is changing a row at that moment, the UPDATE waits for the owner's commit and
   then checks the WHERE again on the NEW row (READ COMMITTED). A link the owner just
   extended no longer matches and is not archived. Selecting ids first would use an old
   snapshot and switch off the freshly extended link. Also: one statement = one round trip.
9. Liveness: "is the process answering?" - on failure the orchestrator restarts it.
   Readiness: "can it serve now?" (DB, Redis) - on failure it gets no traffic, no restart.
   Docker's `HEALTHCHECK` uses `/health/live`. If it used readiness, a database outage
   would make every app container "unhealthy" and get restarted again and again (a
   restart storm), which never fixes the database.
10. 8 x (3 + 2) + 2 x 2 = 44 connections. Budget = 100 - 10 = 90, so it fits. If it did
    not (e.g. 40 workers: 204), Gunicorn refuses to start (`gunicorn.conf.py` raises
    `SystemExit` with the calculation), so the mistake shows at deploy time instead of as
    "too many clients already" at peak traffic.

</details>

## Phase 8 — Brief fixes after the final review

A line-by-line check against the Module 8 brief and learning objectives found two things.

**1. The brief's exact task call failed.** The brief writes
`track_click_task.delay(url_id, ip_address, user_agent)`; our task required a 4th
argument (`referrer`), so that exact call raised `TypeError`. Now `referrer` and
`clicked_at` are optional: the brief's call works word for word (a test makes exactly
that call, and it was run on the live worker), and the API still sends all five values.

**2. The health checks waited one after the other.** FastAPI resolves dependencies in
sequence, so DB, replica and Redis checks added up (test: two 0.5 s checks = 1.02 s).
Now the route starts all three with `asyncio.gather` and waits for all of them: the
slowest one decides (0.5 s). This is the async/await pattern of the first learning
objective: while one check waits for the network, the others run.

```python
database_ok, replica_ok, cache_ok = await asyncio.gather(
    database_is_up(db),
    replica_is_up(read_db, settings.database_replica_url),
    cache_is_up(cache),
)
```
Each check uses its own session (one connection each): a single connection can never run
two queries at the same time.

| Choice | Gain | Cost |
|---|---|---|
| Optional `referrer` / `clicked_at` | The brief's call works; old messages in the queue still work | Two ways to call the task |
| `asyncio.gather` for the checks | Probe time = slowest check, not the sum | Two database connections at once during a check (primary + replica) |

## Phase 9 — Geo-location of clicks (in the worker)

**Goal.** The brief's analytics promise "geo-location", but `clicks.country` and
`clicks.city` were never filled (every click showed "Unknown"). Now the Celery worker
looks the visitor's IP up in a MaxMind database while it saves the click. Like a post
office reading the postcode on each letter in the back room: the customer at the counter
(the visitor) never waits for it.

**How**
1. `GEOIP_DATABASE_PATH` points to a GeoLite2-City `.mmdb` file (free MaxMind account;
   put it in `./geoip/`, mounted read-only into the worker; git-ignored).
2. `get_geoip_reader()` opens it ONCE per worker process (memory-mapped, `lru_cache`),
   on the first click, like the database engine. Missing or broken file = one warning,
   then `None`.
3. `locate(reader, ip)` returns `Location(country, city)`: ISO code ("GB") and the English
   city name. Private or unknown IPs, a missing database, a failed lookup = `UNKNOWN`.
   It never raises: a location is extra, the click itself must always be saved.
4. `click_statements(event, country, city)` puts them in the same INSERT.

**Seen live** (worker pointed at MaxMind's test database for the check):
`81.2.69.142 -> GB London`, `89.160.20.112 -> SE Linköping`, `2001:218::1 -> JP (no city)`,
`10.0.0.1 -> unknown`. A first attempt had a wrong path (Git Bash rewrote `/app/...`): the
worker logged "GeoIP database not usable" and still saved every click, which shows the
fallback working.

**What changed**

| File | Change |
|---|---|
| `app/worker/geoip.py` | New: `get_geoip_reader()`, `locate()`, `Location` |
| `app/worker/tasks.py` | Looks the IP up before the INSERT |
| `app/services/clicks.py` | `click_statements(event, country, city)` |
| `app/core/config.py`, `.env.example` | `GEOIP_DATABASE_PATH` |
| `requirements.txt` | `maxminddb` (no dependencies; not `geoip2`, which pulls in aiohttp and requests) |
| `docker-compose*.yml`, `.gitignore`, `.dockerignore`, `geoip/README.md` | Read-only `./geoip` mount; the licensed file never goes into git or the image |
| `tests/test_geoip.py`, `tests/data/` | MaxMind's public test database (MIT/Apache), 15 tests |

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| Lookup in the worker, not the redirect | The visitor never waits | The location appears with the click, a moment later |
| Local `.mmdb` file (not a web API) | Microseconds per lookup, no network, no per-request fee, IPs never leave our servers | A file to download and update weekly |
| `maxminddb` instead of `geoip2` | No extra dependencies | We read the record's fields ourselves (a few lines) |
| Never raise from `locate()` | A geo problem never costs a click | A broken setup only shows as warnings + "Unknown" |
| Only the country code and city name are stored | Small rows; fits the columns | No region, latitude/longitude |
| Location at click time is stored | History stays true when IP blocks move | Old clicks are not corrected by later database updates |

## Phase 10 — Prometheus metrics

**Goal.** Logs tell the story of ONE request; metrics count ALL of them. Prometheus
reads (scrapes) `GET /metrics` every few seconds and keeps the numbers over time, so we
get graphs ("p95 latency of redirects today") and alerts ("clicks are being lost").
Like a car dashboard: the logbook says what happened on each trip; the dashboard shows
speed and fuel right now, and a warning light turns on by itself.

**What is measured**

| Metric | Type | Why |
|---|---|---|
| `http_requests_total{method,route,status}` | counter (only goes up) | Traffic and error rate per endpoint |
| `http_request_duration_seconds{method,route}` | histogram (buckets) | Percentiles: p95 = 95% of requests are faster |
| `http_requests_in_progress{method}` | gauge (up and down) | Load right now |
| `redirect_cache_lookups_total{result}` | counter | Hit ratio; Redis errors |
| `click_events_total{outcome}` | counter | `lost > 0` = the broker is down: alert |

**Three things that are easy to get wrong**
1. **Cardinality.** Every label combination is a new time series. A `path` label with
   real short codes = one series per link (millions), and the codes leak. We use the
   route template from the router (`scope["route"].path`); no match = `unmatched`;
   unknown HTTP methods = `other`. A scanner cannot create new series.
2. **Several Gunicorn workers.** Each has its own counters. A scrape reaches ONE random
   worker, so the numbers jump around. Multiprocess mode: `gunicorn.conf.py` sets
   `PROMETHEUS_MULTIPROC_DIR` before the workers start, each worker writes files there,
   `/metrics` adds them up. `on_starting` clears old files; `child_exit` removes a dead
   worker's live gauge. Seen live: 3 workers counted 4, 16 and 12; one scrape showed 32.
3. **Exposure.** `/metrics` shows internals (routes, traffic). Keep it on an internal
   network, or set `METRICS_TOKEN` (compared with `hmac.compare_digest`). An empty
   `METRICS_TOKEN=` means "no token" (a test guards this), not an empty secret.

**Seen live with Prometheus** (`docker compose --profile monitoring up -d prometheus`):
target up; `sum by (route, status) (http_requests_total)` showed 20 redirects under
`/{short_code}`, the scanner probe under `unmatched`; cache 1 miss + 19 hits; 20 clicks queued.

**What changed**

| File | Change |
|---|---|
| `app/core/metrics.py` | New: the metrics, `method_label`, `render_metrics` (multiprocess aware) |
| `app/core/middleware.py`, `app/main.py` | `MetricsMiddleware` (outermost: counts 413s and crashes too) |
| `app/api/routes/metrics.py` | `GET /metrics` (hidden from Swagger), optional Bearer token, `no-store` |
| `app/cache/redirect_cache.py`, `app/worker/tracker.py` | hit / miss / error and queued / lost counters |
| `app/core/config.py`, `.env.example` | `METRICS_TOKEN` (empty = none) |
| `app/schemas/url.py` | `metrics` is a reserved alias (the link would hide behind `/metrics`) |
| `gunicorn.conf.py`, `pyproject.toml` | Multiprocess folder + `on_starting` / `child_exit` hooks |
| `docker/prometheus/prometheus.yml`, `docker-compose.yml` | Optional Prometheus (profile `monitoring`) with example queries |
| tests | `test_metrics.py` (incl. two real processes added up), Gunicorn hook tests |

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| Route template labels | Bounded number of series; no codes leak | No per-link numbers in Prometheus (analytics has them) |
| Histogram with fixed buckets | Percentiles over any time range, across workers | Approximate (within a bucket); fixed bucket choice |
| Multiprocess mode | Correct totals for the whole server | Metric files on disk; gauges need a mode (`livesum`) |
| Metrics in the API process only | Simple | Worker-side numbers come from logs (or a Celery exporter) |
| Optional token instead of always-on auth | Easy local use | Production must set it or keep `/metrics` internal |

## Phase 11 — OpenTelemetry tracing

**Goal.** Logs describe one moment, metrics count everything; a TRACE follows ONE request
through every part of the system and shows where its time went. Like a parcel's tracking
history: picked up 09:02, sorting centre 09:40, on the van 11:15, delivered 11:58. Each
line is a SPAN (a step with a start and a duration); together they are the TRACE.

**One real redirect, as Jaeger showed it** (15 spans, 2 services, 1 trace id)
```text
api     GET /{short_code}             77.6 ms   (the visitor's request)
api       GET  (Redis, cache miss)     7.8 ms
api       connect (PostgreSQL)        26.7 ms   first request: a new pool connection
api       SELECT                      10.5 ms
api       SET  (Redis, fill cache)     0.8 ms   -> 302 sent at ~78 ms
api     BackgroundTask track         180.9 ms   AFTER the response
api       apply_async/clicks.track_click   28 ms (LPUSH onto the queue)
worker  run/clicks.track_click       335.2 ms   (same trace: "traceparent" in the message)
worker    connect                     40.5 ms   first task of this process
worker    INSERT                      14.2 ms
worker    UPDATE                       2.2 ms
```
What the trace teaches: the visitor waited 78 ms; the 181 ms background task and the
335 ms worker task did not slow them down. The worker's task was mostly warm-up (first
connection), its SQL only 16 ms.

**How**
- `setup_tracing(settings, "api" | "worker")`: a `TracerProvider` with the service name,
  a sampler (`ParentBased(TraceIdRatioBased(ratio))`) and a `BatchSpanProcessor` that sends
  spans over OTLP/HTTP in the background. Returns `None` (tracing off) without an endpoint.
- API: FastAPI (route template as span name; `/health*`, `/metrics` and the ASGI
  send/receive steps excluded), SQLAlchemy (both engines), Redis, Celery (sending side).
- Worker: set up in `worker_process_init`, AFTER Celery forks (the exporter's background
  thread does not survive a fork); flushed in `worker_process_shutdown`.
- Logs: `RequestContextFilter` adds `trace_id` / `span_id` when a span is active.

**What changed**

| File | Change |
|---|---|
| `app/core/tracing.py` | New: provider setup, API/worker/engine instrumentation |
| `app/main.py` | Tracing with the app; engines instrumented in the lifespan; flush on shutdown |
| `app/worker/celery_app.py`, `app/worker/db.py` | Per-process worker tracing; its engine instrumented |
| `app/core/logging.py` | `trace_id` / `span_id` on traced log lines |
| `app/core/config.py`, `.env.example`, compose files | `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_TRACES_SAMPLER_RATIO`; optional Jaeger (profile `tracing`) |
| `requirements*.txt` | OpenTelemetry SDK, OTLP exporter, 4 instrumentations (+ `certifi` moved to runtime) |
| `tests/test_tracing.py` | In-memory exporter: span names, same trace for SQL, no values in SQL, exclusions, trace id in logs, `traceparent` in the Celery message, worker setup/flush |

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| OpenTelemetry (vendor-neutral) | Any backend (Jaeger, Tempo, Honeycomb, Datadog) without code changes | Many packages; instrumentations are still 0.x beta |
| Off unless an endpoint is set | Zero cost in tests and simple setups | Must be switched on per environment |
| Batch export in the background | Requests never wait for the tracing backend | Spans of a crashed process can be lost; flushed on clean shutdown |
| `ParentBased` ratio sampling (prod 10%) | Cheap under heavy load; a kept trace is never cut in half | 90% of requests have no trace (logs and metrics still see them) |
| Route template as span name, SQL placeholders only | No codes or values leak; bounded names | The real path is still in span attributes (traces are access-controlled) |
| Trace id in the log lines | Jump from a log line to the trace and back | Two more fields on traced lines |

## Phase 12 — Profiling, load testing and thread allocation

**Goal.** Measure instead of guessing: where does a redirect spend its time (latency),
which functions use the CPU, does memory grow (a leak), and how does the server behave
with 50 people at once. Like a doctor's check-up: the pulse at rest (profile), then the
stress test on the treadmill (load test). Results: `docs/learning/module-8-profile.md`.

**Tools**

| Question | Tool | What it shows |
|---|---|---|
| How long do requests take? | `time.perf_counter()` + percentiles | p50 = typical, p95/p99 = the slow tail real users feel |
| Where does the CPU go? | `cProfile` (`tottime` = own time) | Functions that WORK; waiting for the network is not CPU |
| Does memory grow? | `tracemalloc` snapshots compared | Allocations per source line; growth per request |
| How does it hold under load? | Locust (50 users, Docker) | Requests/s, percentiles and errors per kind of request |

**What the measurements taught (and what changed because of them)**
1. A cache hit is ~2.7x faster than a miss (5.2 vs 14.0 ms p50).
2. The profiler itself was wrong at first: `app.dependency_overrides` makes FastAPI
   re-inspect the override on every request. Fixed by swapping `app.state.click_tracker`.
3. The profiler could silently measure nonsense: Redis unreachable (both rows = timeouts) or
   a tombstone from a previous run (the "hit" row = misses). It now refuses both, clearly.
4. `localhost` on Windows = IPv6 first = a 0.5 s Redis timeout on every call: use 127.0.0.1.
5. No memory leak (3x the requests, +27% memory: caches filling up).
6. Load test: worker recycling every 1000 requests caused 8 connection resets and the slow
   tail. With no leak, recycling became rare (10,000): 0 errors, p99 710 -> 290 ms.
7. Gunicorn 26's control socket failed on every worker start (no home folder for the app
   user); not used, so switched off.

**Thread allocation**

| Pool | Size | Runs |
|---|---|---|
| Event loop (1 per worker) | - | All async work: thousands of waiting requests at once |
| AnyIO default pool | `THREAD_POOL_SIZE` = 40 | Blocking I/O: `.delay()`, metrics files. Threads mostly wait |
| Password pool (Module 7) | 4 | Argon2: CPU + 64 MB each, so few at a time |
| Gunicorn `threads` | not used | Only for sync/gthread workers; a UvicornWorker ignores it |

When a pool is full, new blocking work WAITS (a test shows 4 jobs on 2 threads = two
rounds). That is why publishing to the broker fails fast: a hanging broker must not hold
all 40 threads.

**What changed**

| File | Change |
|---|---|
| `app/scripts/profile_redirects.py` | New: latency percentiles, cProfile, tracemalloc; refuses an unreachable Redis or an unfilled cache; restores log levels |
| `loadtest/locustfile.py`, `docker-compose.yml` | Locust scenarios (hit / miss / readiness) run from Locust's Docker image (profile `loadtest`) |
| `gunicorn.conf.py` | `max_requests` 10,000 + 1,000 jitter; control socket off; why no `threads` |
| `app/core/config.py`, `app/main.py`, `.env.example` | `THREAD_POOL_SIZE` set in the lifespan; `localhost` warning |
| `pyproject.toml` | `loadtest/` excluded from ruff (Locust is not installed locally) |
| tests | `test_profiling.py` (18), Gunicorn recycling / control socket / no threads |

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| Profile in process (no network) | Shows our code's cost, repeatable | Real users also pay network and proxy time |
| Three separate passes | Profilers do not distort the latency numbers | Takes three times as long |
| Locust from Docker | No gevent/Flask in our dependencies | Docker needed to run it |
| Rare worker recycling | No resets or cold workers under load | A real leak would grow longer before a restart |
| `THREAD_POOL_SIZE` 40 | Many blocking waits at once, cheap | Too high could hide a slow dependency; too low queues work |

## Phase 13 — Startup probe and Kubernetes manifests

**Goal.** Let Kubernetes heal the app by itself, and never send a request to a pod that
cannot answer it. Three questions, three probes. Like opening a shop each morning:
STARTUP = "is the shop set up yet?" (do not judge it before it opened), LIVENESS = "is
anyone inside?" (if not, send someone new), READINESS = "can it serve right now?" (if
the till is broken, send customers to the other shop, but do not fire the staff).

| Probe | Ours | Fails -> |
|---|---|---|
| startup | `/health/startup`: DB answers AND `alembic_version` = the newest migration | wait (max 150 s) |
| liveness | `/health/live`: nothing but the process | restart the pod |
| readiness | `/health/ready`: PostgreSQL + Redis (concurrently) | no traffic, no restart |

**Why the startup probe checks the migration.** A rolling deploy starts new pods with new
code. If that code expects a column the migration Job has not added yet, every request
would fail. The new pod stays "starting" (no traffic, no liveness restarts) until the
schema is current; the old pods keep serving meanwhile.

**Details that matter (each one is tested in tests/test_kubernetes.py)**
- Probe timeouts (3 s) longer than the app's own checks (2 s).
- Readiness reacts in 10 s, liveness only after 30 s: stop traffic fast, restart late.
- `maxUnavailable: 0`: a deploy never drops below the wanted API pods.
- Beat: 1 replica + `Recreate` (a rolling update would briefly run two beats).
- `preStop: sleep 5` + `terminationGracePeriodSeconds: 45` (> Gunicorn's 30 s).
- `WEB_CONCURRENCY` explicit: `os.cpu_count()` in a pod sees the NODE (64 cores = 129 workers).
- Connection math over ALL pods: 2 x 2 x (3+2) + 1 x 2 x 2 = 24 <= 100 - 10.
- Memory limit 1 GiB: 2 workers x 4 Argon2 hashes x 64 MB = 512 MB at login peaks.
- Security: user 999 (numeric, so `runAsNonRoot` can be checked), read-only root, `/tmp`
  as emptyDir, no privilege escalation, all capabilities dropped; the Secret is never in
  the kustomization (placeholders in `secret.example.yaml`).

**Checked for real**
- kubeconform (official Kubernetes 1.33 schemas, strict): 8 valid, 0 invalid (also in CI).
- `kubectl kustomize deploy/k8s`: renders 7 objects, no Secret.
- The image run like the pod (read-only root, tmpfs /tmp, user 999): `/health/startup`,
  `/health/live`, `/health/ready` all 200, 0 warnings; the worker answered ping.

**What changed**

| File | Change |
|---|---|
| `app/db/migrations.py` | New: expected head (read once), current revision |
| `app/api/routes/health.py` | `/health/startup` |
| `deploy/k8s/*.yaml` | ConfigMap, Secret example, migration Job, API (+ Service, PDB), worker, beat, kustomization |
| `.github/workflows/ci.yml` | kubeconform step |
| `.dockerignore` | `deploy/`, `loadtest/` not in the image |
| tests | `test_health_startup.py` (6), `test_kubernetes.py` (25) |

**Tradeoffs**

| Choice | Gain | Cost |
|---|---|---|
| Startup = schema current | No pod serves with code newer than the schema | Pods wait for the Job; a forgotten Job blocks the rollout (visible) |
| Plain manifests + kustomize | Readable, no extra tool to learn | No templating per environment (Helm would add it) |
| No CPU limit, only a request | No CPU throttling of latency-sensitive requests | A busy pod can use spare node CPU (memory IS limited) |
| Worker liveness via `inspect ping` | Proves the worker reads the broker | Starts Python every 60 s |
| Managed PostgreSQL/Redis (not in the manifests) | Backups, failover by the provider | Not runnable on a bare cluster without them |
