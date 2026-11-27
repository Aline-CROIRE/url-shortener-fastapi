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
