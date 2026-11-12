# URL Shortener (FastAPI + SQLAlchemy 2.0 + PostgreSQL)

A URL shortener microservice. Users create short links (random codes or custom aliases)
with optional expiry dates and tags; every visit of a short link is redirected and recorded,
and click analytics are computed in SQL.

Module 6 adds the **ORM and data access layer** on top of the Module 5 service:
async SQLAlchemy 2.0 models, Alembic migrations, repositories, transactions, keyset
pagination, SQL aggregation, N+1 prevention, a Redis cache and an optional read replica.

> **FastAPI instead of Django.** The course is written for Django; the instructor approved
> FastAPI for this project. The table below maps each Django concept to its equivalent here.

| Django | This project |
|---|---|
| `models.Model` | SQLAlchemy 2.0 models (`DeclarativeBase`, `Mapped[...]`, `mapped_column()`) in [app/models/](app/models/) |
| `makemigrations` / `migrate` | Alembic: `alembic revision --autogenerate`, `alembic upgrade head` |
| Model `Manager` (`URL.objects.active()`) | Repository classes in [app/repositories/](app/repositories/) (`URLRepository.active_urls()`) |
| `select_related()` | `joinedload()` |
| `prefetch_related()` | `selectinload()` |
| `transaction.atomic()` | `async with session.begin():` in the service layer |
| `F("click_count") + 1` | `update(URL).values(click_count=URL.click_count + 1)` |
| Serializers / forms | Pydantic v2 schemas in [app/schemas/](app/schemas/) |
| `annotate()` / `aggregate()` | `func.count()`, `group_by()`, `case()`, `.label()` |

---

## Contents

- [Features](#features)
- [Tech stack](#tech-stack)
- [Architecture](#architecture)
- [Database (ER diagram)](#database-er-diagram)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Migrations](#migrations)
- [Seed data and query plans](#seed-data-and-query-plans)
- [API reference](#api-reference)
- [Redis cache](#redis-cache)
- [Read replica](#read-replica)
- [Testing](#testing)
- [Design decisions](#design-decisions)
- [Project structure](#project-structure)
- [What comes next](#what-comes-next)

## Features

- Create, read, update and soft-delete short URLs (owner, custom alias, expiry, tags)
- Redirect `GET /{short_code}` for live links only (404 unknown, 410 expired / switched off / deleted)
- Every visit records a click (IP, user agent, referrer) and increments the counter in **one transaction**
- URL list with **keyset (cursor) pagination**, sorting and optional filters
- Click analytics per country, city, UTC day and referrer, unique visitors, first/last click (all computed by PostgreSQL)
- Redis **cache-aside** for redirects (15-minute TTL, invalidation, works without Redis)
- Optional **read replica**: read-only endpoints use a replica, writes use the primary
- One JSON error shape for every error: `{"error": {"code", "message", "details"}}`
- 288 tests, 100% line and branch coverage

## Tech stack

| Part | Choice |
|---|---|
| Language | Python 3.14 |
| Web framework | FastAPI, Uvicorn (dev), Gunicorn + Uvicorn workers (production) |
| ORM | SQLAlchemy 2.0, **async** (`AsyncSession`) with the `asyncpg` driver |
| Migrations | Alembic |
| Validation | Pydantic v2, pydantic-settings |
| Database | PostgreSQL 17 (any 15+ works) |
| Cache | Redis 8 |
| Tests | pytest, pytest-cov, FastAPI `TestClient` |
| Containers | Docker, Docker Compose |

## Architecture

```mermaid
flowchart LR
    client([Client]) --> routes

    subgraph app [FastAPI app]
        routes["Routes<br/>app/api/routes"] --> services["Services<br/>business rules +<br/>transactions"]
        services --> repos["Repositories<br/>all SQL queries"]
        services --> cache["Redirect cache<br/>(cache-aside)"]
    end

    repos -- "writes, redirects" --> primary[("PostgreSQL<br/>primary")]
    repos -- "read-only GETs" --> replica[("PostgreSQL<br/>replica (optional)")]
    primary -. "streaming replication" .-> replica
    cache --> redis[("Redis")]
```

- **Routes** are thin: they validate input (Pydantic), call a service and shape the response.
- **Services** hold the rules (404/410, aliases, soft delete) and own the **transactions**.
- **Repositories** are the only place with queries. They add and flush, but never commit.
- **Dependencies** (`Depends`) give each request one database session and build the repositories and services on it.

## Database (ER diagram)

```mermaid
erDiagram
    USERS ||--o{ URLS : owns
    URLS ||--o{ CLICKS : receives
    URLS ||--o{ URL_TAGS : "tagged with"
    TAGS ||--o{ URL_TAGS : "used by"

    USERS {
        int id PK
        string email UK "lower-case"
        string username UK
        string hashed_password "used from Module 7"
        bool is_premium "default false"
        enum tier "free | premium | admin"
        timestamptz created_at
        timestamptz updated_at
    }
    URLS {
        int id PK
        int owner_id FK "ON DELETE CASCADE"
        string short_code UK "max 10, indexed"
        string custom_alias UK "nullable, equals short_code"
        text original_url
        bool is_active "default true"
        timestamptz expires_at "nullable"
        timestamptz deleted_at "soft delete, nullable"
        string title "nullable (Module 9)"
        text description "nullable (Module 9)"
        text favicon "nullable (Module 9)"
        int click_count "default 0, CHECK >= 0"
        timestamptz created_at "indexed"
        timestamptz updated_at
    }
    CLICKS {
        bigint id PK
        int url_id FK "ON DELETE CASCADE"
        timestamptz clicked_at "set by the database"
        inet ip_address "nullable"
        text user_agent "nullable"
        text referrer "nullable"
        string country "ISO code, nullable"
        string city "nullable"
    }
    TAGS {
        int id PK
        string name UK "unique ignoring case"
        timestamptz created_at
        timestamptz updated_at
    }
    URL_TAGS {
        int url_id PK,FK "ON DELETE CASCADE"
        int tag_id PK,FK "ON DELETE CASCADE"
    }
```

Deleting a user removes their URLs, clicks and tag links in one statement (`ON DELETE CASCADE`);
tags are shared and stay.

**Indexes**

| Index | Columns | Used by |
|---|---|---|
| `ix_urls_short_code` (unique) | `short_code` | redirect and URL lookups |
| `ix_urls_created_at` | `created_at` | "newest first" ordering |
| `ix_urls_owner_id_created_at_id` | `owner_id, created_at, id` | the URL list (keyset pagination); also serves `owner_id` alone |
| `uq_urls_custom_alias` (unique) | `custom_alias` | alias uniqueness |
| `ix_clicks_url_id_clicked_at` | `url_id, clicked_at` | all analytics queries; also serves `url_id` alone |
| `uq_tags_name`, `uq_tags_name_lower` (unique) | `name`, `lower(name)` | tag lookups, case-insensitive uniqueness |
| `pk_url_tags` + `ix_url_tags_tag_id` | `(url_id, tag_id)`, `tag_id` | tags of a URL / URLs with a tag |

## Quick start

### With Docker (everything included)

```bash
cp .env.example .env              # optional: compose has working defaults
docker compose up --build
```

This starts PostgreSQL, Redis, a one-off `migrate` container (`alembic upgrade head`) and the
app with hot reload. The app starts only after the migrations succeeded.

- Swagger UI: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

Production-like run (Gunicorn with several Uvicorn workers, no code mount, password required):

```bash
POSTGRES_PASSWORD=change-me docker compose -f docker-compose.prod.yml up --build
```

### Locally (app on your machine, databases in Docker)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
Copy-Item .env.example .env           # Linux/macOS: cp .env.example .env

docker compose up -d db redis         # PostgreSQL (+ the test database) and Redis
alembic upgrade head                  # create the tables
uvicorn app.main:app --reload
```

Paths have **no trailing slash** (`/api/v1/urls`, `/{short_code}`). A request with a trailing
slash receives a `307` redirect to the canonical path (method and body are kept).

## Configuration

All settings come from environment variables (or `.env`), read by pydantic-settings in
[app/core/config.py](app/core/config.py). See [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `APP_NAME` | `URL Shortener` | title shown in Swagger |
| `APP_VERSION` | `0.1.0` | version shown in Swagger |
| `ENVIRONMENT` | `development` | `development`, `production` or `testing` |
| `DEBUG` | `false` | more detailed logs (never sends stack traces to clients) |
| `BASE_URL` | `http://localhost:8000` | used to build `short_url` |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/url_shortener` | primary database (writes) |
| `DATABASE_REPLICA_URL` | *(empty)* | read replica for GET endpoints; empty = reads use the primary |
| `TEST_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/url_shortener_test` | used only by the tests |
| `DB_ECHO` | `false` | log every SQL statement |
| `REDIS_URL` | *(empty)* | redirect cache; empty = no cache |
| `CACHE_TTL_SECONDS` | `900` | maximum time a redirect stays cached (15 minutes) |
| `WORKERS` | `2` | Gunicorn worker processes (production image) |

## Migrations

The schema is created **only** by Alembic (the app never calls `create_all()`).
[alembic/env.py](alembic/env.py) reads `DATABASE_URL` from the settings and `Base.metadata` from the models.

```bash
alembic upgrade head                                 # apply everything
alembic downgrade -1                                 # undo the newest migration
alembic revision --autogenerate -m "describe change" # draft a migration from model changes
alembic upgrade head --sql                           # print the SQL without running it
alembic current / alembic history / alembic check    # where am I / the chain / models == migrations?
```

| Revision | Migration | Kind |
|---|---|---|
| `dca376ab17be` | create users table (+ `user_tier` enum) | schema |
| `2ccea71a651f` | create urls table | schema |
| `d488552c333d` | create clicks table | schema |
| `f113c0993510` | create tags and url_tags tables | schema |
| `5de473cbc932` | seed default tags (Marketing, Social, Email, Ads, Events, Personal) | data |
| `2b62d0fc3bea` | add `deleted_at` to urls (soft delete) | schema |
| `0109315884f6` | list index on `(owner_id, created_at, id)` | schema |

Rules followed:

- Every autogenerated migration is **reviewed** before use (for example, autogenerate forgets
  to drop the PostgreSQL enum type in `downgrade`).
- Every migration has a working `downgrade`; the tests run `downgrade base` and `upgrade head`.
- Data migrations use a frozen `sa.table(...)` copy, never the application models.
- Tests check that the models match the migrations and that there is exactly one head.
- In Docker, a separate `migrate` container runs the migrations once before the app starts.

## Seed data and query plans

```bash
python -m app.scripts.seed                 # 50 users, 10,000 URLs, 200,000 clicks (one transaction)
python -m app.scripts.seed --reset         # replace earlier seed data
python -m app.scripts.seed --urls 500 --clicks 5000 --seed 7
python -m app.scripts.explain              # EXPLAIN (ANALYZE, BUFFERS) on the key queries
```

The seed script bulk-inserts with `insert()` in batches inside **one transaction**, keeps each
URL's `click_count` equal to its real number of clicks, marks its users with `@seed.local`
(so `--reset` removes only seed data) and refuses to run when `ENVIRONMENT=production`.

Measured on the seeded database (10,002 URLs, 200,000 clicks):

| Query | Plan | Time |
|---|---|---|
| Redirect lookup by short code | Index Scan using `ix_urls_short_code` | 0.04 ms |
| URL list, page 1 | Index Scan Backward using `ix_urls_owner_id_created_at_id` | 0.16 ms |
| URL list, page 6 (cursor) | same index, the cursor is part of the index condition | 0.15 ms |
| Analytics summary (9,295 clicks) | Bitmap Index Scan on `ix_clicks_url_id_clicked_at` | ~11 ms |
| Clicks per UTC day | Index Only Scan, 0 heap fetches | 1.5 ms |
| OFFSET pagination, page 10,000 (for comparison) | reads and discards 199,980 rows | 46.8 ms |

## API reference

Interactive documentation: **`/docs`** (Swagger UI) and `/openapi.json`.

| Method | Path | Success | Errors |
|---|---|---|---|
| `GET` | `/health` | 200 | 503 |
| `POST` | `/api/v1/urls` | 201 | 409, 422 |
| `GET` | `/api/v1/urls` | 200 | 400, 422 |
| `GET` | `/api/v1/urls/{short_code}` | 200 | 404, 422 |
| `PUT` | `/api/v1/urls/{short_code}` | 200 | 404, 422 |
| `DELETE` | `/api/v1/urls/{short_code}` | 204 | 404, 422 |
| `GET` | `/api/v1/analytics/{short_code}` | 200 | 404, 422 |
| `GET` | `/{short_code}` | 302 | 404, 410, 422 |

### Create a short URL

```bash
curl -X POST http://localhost:8000/api/v1/urls \
  -H "Content-Type: application/json" \
  -d '{"original_url": "https://www.python.org/about/", "custom_alias": "py-about",
       "expires_at": "2027-01-31T23:59:59Z", "tags": ["Marketing", "Social"]}'
```

- `custom_alias` (optional): 3–10 letters, digits, `-` or `_`; it becomes the short code.
  Reserved words (`api`, `docs`, `health`, ...) are refused. A taken alias returns `409 alias_taken`.
- `expires_at` (optional): must include a time zone and be in the future.
- `tags` (optional): up to 10, trimmed, duplicates (ignoring case) removed, created if new.
- The URL and its tags are saved in one transaction.

The response contains the owner, tags, `click_count` and `short_url`.

### List your URLs (keyset pagination)

```bash
curl "http://localhost:8000/api/v1/urls?limit=20&sort=newest&tag=marketing&search=python"
# next page: add &cursor=<next_cursor from the previous response>
```

| Parameter | Values |
|---|---|
| `limit` | 1–100, default 20 |
| `cursor` | `next_cursor` of the previous page |
| `sort` | `newest` (`created_at`, `id`) or `most_clicked` (`click_count`, `id`) |
| `tag` | tag name, any letter case |
| `is_active` | `true` / `false` |
| `expired` | `true` / `false` |
| `search` | text in the URL or title (case-insensitive) |

Response: `{"items": [...], "next_cursor": "..." | null}`. Filters are combined and only
applied when given. A broken cursor, or one from another sort order, returns `400 invalid_cursor`.

### Update and delete

- `PUT /api/v1/urls/{short_code}` changes **only the fields you send** (`original_url`,
  `is_active`, `expires_at`, `title`, `description`, `tags`). `tags` replaces all tags;
  `"expires_at": null` removes the expiry. The short code and alias cannot change.
- `DELETE /api/v1/urls/{short_code}` is a **soft delete**: the link returns `410 url_deleted`,
  disappears from the list, keeps its click history, and its code is never given to anyone else.

### Analytics

```bash
curl "http://localhost:8000/api/v1/analytics/py-about?days=30"
```

Returns `total_clicks`, `unique_visitors` (distinct IP addresses, an estimate),
`first_click_at`, `last_click_at`, `clicks_per_country`, `clicks_per_city`,
`clicks_per_day` (UTC days, last `days` days, 1–365) and the top 10 `top_referrers`.
Missing values are grouped as `"Unknown"` (country, city) or `"Direct"` (referrer).

### Errors

Every error has the same shape:

```json
{"error": {"code": "url_expired", "message": "Short URL 'py-about' is expired and no longer redirects.", "details": {"short_code": "py-about"}}}
```

| Status | `code` |
|---|---|
| 400 | `invalid_cursor` |
| 404 | `url_not_found`, `not_found` |
| 405 | `method_not_allowed` |
| 409 | `alias_taken`, `conflict` (with the constraint name in `details`) |
| 410 | `url_expired`, `url_inactive`, `url_deleted` |
| 422 | `validation_error` (with the field errors in `details`) |
| 500 | `internal_server_error` (no internal details), `short_code_generation_failed` |

### Owner of the URLs

Authentication arrives in Module 7. Until then every request acts as one demo user, created
automatically on first use (`app/api/owner.py`, marked `TODO(module-7)`). The URL endpoints
only ever show the current owner's URLs; another owner's code returns 404.

## Redis cache

Redirects use the **cache-aside** pattern ([app/cache/redirect_cache.py](app/cache/redirect_cache.py)):

1. look up `redirect:v1:{short_code}` in Redis;
2. hit: record the click (2 SQL statements instead of 3) and redirect;
3. miss: read PostgreSQL, record the click, then cache the link (only live links).

- TTL: 15 minutes, or less if the link expires sooner.
- Invalidation after a successful update or delete.
- If Redis is unavailable the app keeps working from the database (it logs a warning);
  `/health` shows `"cache": "down"` but stays healthy.
- Leave `REDIS_URL` empty to run without a cache.

## Read replica

Read-only endpoints (`GET /api/v1/urls`, `GET /api/v1/urls/{short_code}`,
`GET /api/v1/analytics/{short_code}`) use a **read-only session**; everything else, including
the redirect (it records a click and must see brand-new links), uses the primary.

- Without `DATABASE_REPLICA_URL` the read session uses the primary's connection pool, still
  read-only, so a write on the read path fails immediately during development.
- With a replica, reads may be a few milliseconds behind (replication lag). Create and
  update responses therefore return the full object from the primary.

Run a local streaming replica (Docker profile `replica`, port 5434):

```bash
# once, for a database volume created before replication was added:
docker compose exec db sh /docker-entrypoint-initdb.d/enable-replication.sh
docker compose --profile replica up -d db-replica
# then in .env:
# DATABASE_REPLICA_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5434/url_shortener
```

`/health` shows `"replica": "ok" | "down" | "disabled"`; a configured replica that is down
makes the app unhealthy (503) because the read endpoints need it. In production, a managed
database service usually provides the replica; the app only needs `DATABASE_REPLICA_URL`.

## Testing

```bash
docker compose up -d db              # the tests need PostgreSQL (they use url_shortener_test)
pytest                               # all tests
pytest --cov                         # with coverage (fails below 98%)
pytest --cov --cov-report=html       # HTML report in htmlcov/
```

- The test database is migrated with Alembic at the start of the run; every test runs
  inside a transaction that is **rolled back**, so tests never see each other's data.
- Current result: **288 tests, 100% line and branch coverage**.
- Integration tests run against a real Redis (`docker compose up -d redis`) and a real replica
  (`docker compose --profile replica up -d db-replica`); they are **skipped** automatically
  when those services are not running.
- Highlights: no duplicates or gaps across pages, the "lost update" problem shown with two real
  connections, a failing click insert leaving nothing half-saved, cascade deletes, analytics
  numbers checked exactly, and query counts (the URL list uses 2 queries for 3 or 30 URLs).

## Design decisions

**Async SQLAlchemy.** Module 5 was fully async, so the ORM is async too (`AsyncSession` +
`asyncpg`). All relationships use `lazy="raise"`: anything not loaded on purpose fails loudly
instead of running hidden queries.

**Transactions.** One rule: the **service** opens the transaction (`async with session.begin()`),
repositories only flush, routes never commit. Each visit inserts a click and runs
`click_count = click_count + 1` **in SQL**, in one transaction, so concurrent visits are never
lost (no read-modify-write in Python).

**Keyset pagination instead of OFFSET.** OFFSET reads and discards every skipped row (46.8 ms at
page 10,000 versus 0.16 ms with a cursor) and can repeat or skip rows when data changes between
pages. The cursor holds `(created_at, id)`; `id` breaks ties so every position is unique.

**`joinedload` vs `selectinload`.** `joinedload` for many-to-one (`URL.owner`): a JOIN adds
columns, not rows. `selectinload` for collections (`URL.tags`): one extra `WHERE id IN (...)`
query, because a JOIN would repeat each URL once per tag. The list therefore needs 2 queries
for any number of URLs instead of `1 + 2N` (61 queries for 30 URLs with lazy loading).

**Indexes.** Composite indexes follow the queries: `(owner_id, created_at, id)` for the list and
`(url_id, clicked_at)` for analytics; thanks to the leftmost-prefix rule they also serve
`owner_id` and `url_id` alone, so no separate foreign-key indexes are needed. `click_count` is
deliberately **not** indexed: it changes on every click, so an index would slow down every redirect.

**Soft delete.** Short codes are printed and shared. A hard delete would free the code, and
someone could register it again for a different target (link hijacking). Soft delete keeps the
code reserved and the click history; deleting a whole user still removes everything (cascade).

**Active vs expired.** A link expiring exactly now counts as expired (`expires_at <= now`), so
no link is ever in both lists or in neither. The rule is written once as a hybrid method
(`URL.is_live()`) and used both in Python and in SQL.

**Unique values.** Duplicate aliases or tags are handled by the database constraints (safe
under concurrency); a unique violation becomes a clean `409`.

**Cache-aside and read replica.** Both are optional and degrade gracefully: without Redis the
app reads the database; without a replica it reads the primary.

**No trailing slash.** FastAPI's convention and the shortest possible links; a trailing slash
is redirected with `307`.

## Project structure

```
app/
├── main.py                 # create_app(): lifespan (engines, cache), middleware, routers
├── api/
│   ├── dependencies.py     # sessions (write/read), repositories, services via Depends
│   ├── owner.py            # temporary owner until Module 7 (TODO(module-7))
│   ├── params.py, responses.py, visitor.py
│   └── routes/             # health, urls, analytics, redirect
├── cache/                  # Redis redirect cache (cache-aside)
├── core/                   # settings, exceptions, error handlers, logging, middleware
├── db/                     # Base + TimestampMixin, engines/sessions, query counter, errors
├── models/                 # User, URL, Click, Tag, url_tags
├── repositories/           # all SQL: users, URLs (+ keyset list), clicks (+ analytics), tags
├── schemas/                # Pydantic request/response models
├── scripts/                # seed.py, explain.py (python -m app.scripts.<name>)
├── services/               # URL, redirect and analytics services (transactions live here)
└── utils/codes.py          # random short codes
alembic/                    # env.py + versions/ (7 migrations)
docker/                     # Postgres init scripts (test database, replication), replica start script
tests/                      # 288 tests
Dockerfile, docker-compose.yml, docker-compose.prod.yml, gunicorn.conf.py
```

## What comes next

- **Module 7:** registration and login with JWT (replaces the demo owner), password hashing,
  premium-only analytics, roles, rate limiting.
- **Module 8:** background tasks with Celery (for example recording clicks asynchronously).
- **Module 9:** a URL preview service that fills `title`, `description` and `favicon`.
