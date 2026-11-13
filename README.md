# URL Shortener

A URL shortening service (like Bitly): create short links with an optional custom alias,
expiry date and tags, get redirected when you open them, and see click analytics.

Built with **FastAPI** (approved by the instructor instead of Django), **async SQLAlchemy 2.0**,
**Alembic**, **PostgreSQL** and **Redis**.

## Architecture

### 1. Services (Docker Compose)

```mermaid
flowchart TB
    client(["Client<br/>browser, curl or Swagger UI"])

    subgraph compose["docker compose"]
        migrate["migrate<br/>runs alembic upgrade head once, then exits"]
        app["app<br/>FastAPI on port 8000<br/>(Gunicorn + Uvicorn workers in production)"]
        db[("db<br/>PostgreSQL 17<br/>main database")]
        redis[("redis<br/>Redis 8<br/>redirect cache")]
        replica[("db-replica<br/>read-only copy<br/>optional")]
    end

    client -->|HTTP requests| app
    migrate -->|creates and updates the tables| db
    app -->|reads and writes| db
    app -->|cached redirects| redis
    app -.->|read-only queries, when enabled| replica
    db -.->|streaming replication| replica
```

- **migrate** runs the database migrations first; the **app** starts only when they succeeded.
- **db** stores users, URLs, clicks and tags.
- **redis** remembers popular redirects for 15 minutes so the app can skip a database query.
  If Redis is down the app still works, just a bit slower.
- **db-replica** is optional: when it is enabled, read-only endpoints (list, details,
  analytics) use this copy and all writes go to **db**.

### 2. Inside the app (layers)

```mermaid
flowchart LR
    request(["HTTP request"]) --> routes

    subgraph app["FastAPI app"]
        direction LR
        routes["Routes<br/>app/api/routes<br/>check the input"]
        services["Services<br/>app/services<br/>business rules and transactions"]
        repos["Repositories<br/>app/repositories<br/>SQL queries"]
        models["Models<br/>app/models<br/>database tables"]
        cache["Cache<br/>app/cache"]
    end

    routes --> services
    services --> repos
    repos --> models
    services --> cache
    models --> pg[("PostgreSQL")]
    cache --> rd[("Redis")]
```

| Layer | Folder | Job |
|---|---|---|
| Routes | `app/api/routes` | Receive the request, validate it with a Pydantic schema, return the response |
| Schemas | `app/schemas` | Describe the JSON coming in and going out |
| Services | `app/services` | Business rules (expired, deleted, alias taken, ...) and the database transaction |
| Repositories | `app/repositories` | Every SQL query lives here (like Django model managers) |
| Models | `app/models` | The database tables, written with SQLAlchemy |

Each request gets its own database session through FastAPI dependencies
(`app/api/dependencies.py`), and the session is always closed at the end.

### 3. What happens when someone opens a short link

```mermaid
sequenceDiagram
    autonumber
    actor visitor as Visitor
    participant app as FastAPI app
    participant redis as Redis
    participant db as PostgreSQL

    visitor->>app: GET /{short_code}/
    app->>redis: look up the short code
    alt found in Redis (cache hit)
        redis-->>app: url id and original URL
    else not in Redis (cache miss)
        redis-->>app: nothing
        app->>db: find the URL by short code
        db-->>app: URL row
        Note over app: unknown code gives 404, expired or deleted gives 410
    end
    app->>db: one transaction: save a click and add 1 to click_count
    app->>redis: remember the URL for 15 minutes (after a miss)
    app-->>visitor: 302 redirect to the original URL
```

Saving the click and increasing the counter happen in **one transaction**: either both are
saved or neither is. The counter is increased inside the database
(`click_count = click_count + 1`), so two visits at the same moment are both counted.

## Data model

```mermaid
erDiagram
    USERS ||--o{ URLS : "owns"
    URLS ||--o{ CLICKS : "receives"
    URLS ||--o{ URL_TAGS : "is tagged by"
    TAGS ||--o{ URL_TAGS : "is used by"

    USERS {
        int id PK
        string email UK "required"
        string username UK
        string first_name
        string last_name
        string hashed_password
        boolean is_active "default true"
        boolean is_staff "default false"
        boolean is_superuser "default false"
        boolean is_premium "default false"
        enum tier "free, premium or admin"
        timestamp last_login "nullable"
        timestamp created_at "date joined"
        timestamp updated_at
    }

    URLS {
        int id PK
        int owner_id FK "users.id"
        string original_url "required"
        string short_code UK "max 10 characters, indexed"
        string custom_alias UK "nullable"
        boolean is_active "default true"
        timestamp expires_at "nullable"
        timestamp deleted_at "nullable, soft delete"
        string title "nullable"
        string description "nullable"
        string favicon "nullable"
        int click_count "default 0, never negative"
        timestamp created_at "indexed"
        timestamp updated_at
    }

    CLICKS {
        bigint id PK
        int url_id FK "urls.id"
        timestamp clicked_at "set by the database"
        inet ip_address "nullable"
        string user_agent "nullable"
        string referrer "nullable"
        string country "nullable"
        string city "nullable"
    }

    TAGS {
        int id PK
        string name UK "e.g. Marketing, Social"
        timestamp created_at
        timestamp updated_at
    }

    URL_TAGS {
        int url_id PK, FK "urls.id"
        int tag_id PK, FK "tags.id"
    }
```

How to read it: `PK` = primary key, `FK` = foreign key (points to another table),
`UK` = unique. `||--o{` means **one to many**: one user owns many URLs.

| Relationship | Type | What happens on delete |
|---|---|---|
| users → urls | one user owns many URLs | deleting a user deletes their URLs |
| urls → clicks | one URL has many clicks | deleting a URL deletes its clicks |
| urls ↔ tags | many to many, through `url_tags` | deleting a URL or a tag removes only the link between them |

- A URL is **live** when it is active, not deleted and not expired; only live URLs redirect.
- Deleting a URL through the API is a **soft delete**: `deleted_at` is set, so its short code is
  never reused and its click history is kept.
- `is_premium` always matches `tier` (premium and admin users are premium).
- Six default tags (Marketing, Social, Email, Ads, Events, Personal) are added by a migration.

## Setup

### With Docker

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, runs the database migrations, then starts the app.

- API docs (Swagger): <http://localhost:8000/docs>
- Health check: <http://localhost:8000/health>

Production mode (Gunicorn):

```bash
POSTGRES_PASSWORD=your-password docker compose -f docker-compose.prod.yml up --build
```

### Without Docker for the app

```bash
python -m venv .venv
.venv\Scripts\activate             # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env

docker compose up -d db redis      # PostgreSQL and Redis
alembic upgrade head               # create the tables
uvicorn app.main:app --reload
```

Settings are read from environment variables; see [.env.example](.env.example).

### Database commands

```bash
alembic upgrade head               # apply all migrations
alembic downgrade -1               # undo the last migration
python -m app.scripts.seed         # fill the database with sample data
```

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/urls/` | Create a short URL |
| `GET` | `/api/v1/urls/` | List your URLs |
| `GET` | `/api/v1/urls/{short_code}/` | Get URL details (owner, tags, click count) |
| `PUT` | `/api/v1/urls/{short_code}/` | Update a URL |
| `DELETE` | `/api/v1/urls/{short_code}/` | Delete a URL |
| `GET` | `/api/v1/analytics/{short_code}/` | Click analytics (per country, city, day, referrer) |
| `GET` | `/{short_code}/` | Redirect to the original URL (302) |
| `GET` | `/health` | Database and cache status |

### Create a short URL

```bash
curl -X POST http://localhost:8000/api/v1/urls/ \
  -H "Content-Type: application/json" \
  -d '{"original_url": "https://www.python.org/", "custom_alias": "python", "tags": ["Marketing"]}'
```

`custom_alias`, `expires_at` and `tags` are optional.

### List your URLs

```bash
curl "http://localhost:8000/api/v1/urls/?limit=20&sort=newest&tag=Marketing"
```

Optional filters: `tag`, `is_active`, `expired`, `search`. Sort: `newest` or `most_clicked`.
Results are paginated: pass the returned `next_cursor` as `?cursor=...` to get the next page.

### Errors

All errors use the same format:

```json
{"error": {"code": "url_not_found", "message": "Short URL 'abc1234' was not found.", "details": {"short_code": "abc1234"}}}
```

Common status codes: `404` not found, `409` alias already taken, `410` link expired or
deleted, `422` invalid input.

## Tests

```bash
docker compose up -d db            # the tests need PostgreSQL
pytest                             # run all tests
pytest --cov                       # with a coverage report
```

Code quality (also run automatically on GitHub for every push):

```bash
ruff check .                       # lint, including docstrings
ruff format --check .              # formatting
mypy                               # type checking
```

## Project structure

```
app/
├── api/            # routes and dependencies
├── models/         # database models (User, URL, Click, Tag)
├── schemas/        # request and response models
├── repositories/   # database queries
├── services/       # business logic
├── cache/          # Redis redirect cache
├── core/           # settings, errors, logging
├── db/             # database connection
└── scripts/        # seed data
alembic/            # database migrations
tests/
```
