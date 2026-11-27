"""Profile the redirect: latency percentiles, CPU hot spots and memory, cache hit vs miss.

    python -m app.scripts.profile_redirects --requests 500 --report docs/learning/profile.md

Runs the real app IN THIS PROCESS (no network, no server) against the database and
Redis from your settings, so the numbers show OUR code: routing, cache, SQL, JSON logs.
A development tool: it needs httpx (requirements-dev.txt).

Three passes, so one tool does not distort the others' numbers:
1. LATENCY  - no profiler running: p50 / p95 / p99 for a cache HIT and a cache MISS.
2. CPU      - cProfile: which functions use the CPU ("tottime" = own time, without
              the functions they call). Time spent WAITING for PostgreSQL or Redis is
              not CPU time: the event loop serves others meanwhile.
3. MEMORY   - tracemalloc: what was allocated while serving N requests, and how much
              memory stays per request (a steady growth = a leak).

Clicks are NOT sent to Celery (a no-op tracker): profiling must not fill the analytics
with fake visits, and the click is sent after the response anyway.
"""

import argparse
import asyncio
import cProfile
import io
import logging
import pstats
import statistics
import sys
import time
import tracemalloc
from collections.abc import Awaitable, Callable, Sequence

import httpx
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.main import create_app
from app.models import URL
from app.repositories.url import utc_now
from app.services.clicks import ClickEvent

WARM_UP_REQUESTS = 20  # first requests open connections and fill caches: not measured
TOP_FUNCTIONS = 12
TOP_ALLOCATIONS = 8


class NoClickTracker:
    """Swallows clicks: profiling must not create fake visits."""

    async def track(self, event: ClickEvent) -> None:
        """Do nothing."""


def latency_summary(samples: Sequence[float]) -> dict[str, float]:
    """Return count, mean, p50, p95, p99 and max of durations (seconds) in milliseconds.

    p95 = 95% of the requests were at least this fast. Averages hide the slow tail;
    percentiles show what the unlucky visitors get.
    """
    ms = [sample * 1000 for sample in samples]
    # 99 cut points: cuts[49] = p50, cuts[94] = p95, cuts[98] = p99 (one sample: itself)
    cuts = ms * 99 if len(ms) == 1 else statistics.quantiles(ms, n=100, method="inclusive")
    return {
        "count": len(ms),
        "mean_ms": statistics.fmean(ms),
        "p50_ms": cuts[49],
        "p95_ms": cuts[94],
        "p99_ms": cuts[98],
        "max_ms": max(ms),
    }


async def measure(
    client: httpx.AsyncClient,
    path: str,
    requests: int,
    before_each: Callable[[], Awaitable[None]] | None = None,
    expected_status: int = 302,
) -> list[float]:
    """Send `requests` GETs and return each one's duration in seconds.

    before_each (e.g. "empty the cache") runs before every request and is NOT timed.

    Raises:
        RuntimeError: If a response is not the expected status (we would time an error).
    """
    samples: list[float] = []
    for _ in range(requests):
        if before_each is not None:
            await before_each()
        start = time.perf_counter()  # a monotonic, high-resolution clock
        response = await client.get(path)
        samples.append(time.perf_counter() - start)
        if response.status_code != expected_status:
            raise RuntimeError(f"GET {path} answered {response.status_code}, not {expected_status}")
    return samples


def cpu_table(profile: cProfile.Profile) -> str:
    """Return the functions with the most own CPU time, as text."""
    out = io.StringIO()
    stats = pstats.Stats(profile, stream=out)
    stats.strip_dirs().sort_stats(pstats.SortKey.TIME).print_stats(TOP_FUNCTIONS)
    # Keep the table only (drop pstats' header lines)
    lines = out.getvalue().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("ncalls"))
    return "\n".join(lines[start:]).rstrip()


def memory_table(before: tracemalloc.Snapshot, after: tracemalloc.Snapshot) -> str:
    """Return the source lines whose allocations grew the most between two snapshots."""
    stats = after.compare_to(before, "lineno")[:TOP_ALLOCATIONS]
    return "\n".join(str(stat) for stat in stats)


# Loggers that write one line per request: quiet while profiling, restored afterwards
NOISY_LOGGERS = ("app.requests", "httpx")


async def profile_redirects(settings: Settings, short_code: str, requests: int) -> str:
    """Run the three passes against the redirect of `short_code`; return a Markdown report.

    Raises:
        SystemExit: If Redis is configured but does not answer (the numbers would be fake).
    """
    # One log line per request would flood the output (and is not what we study).
    # Remember the levels: the rest of the process must keep its logging as it was.
    old_levels = {name: logging.getLogger(name).level for name in NOISY_LOGGERS}
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        return await _profile(settings, short_code, requests)
    finally:
        for name, level in old_levels.items():
            logging.getLogger(name).setLevel(level)


async def _profile(settings: Settings, short_code: str, requests: int) -> str:
    app = create_app(settings)
    path = f"/{short_code}"

    async with app.router.lifespan_context(app):  # startup: engine, Redis client, ...
        # Swap the tracker the REAL dependency returns. Not app.dependency_overrides:
        # FastAPI then re-inspects the override's signature on EVERY request, which
        # showed up in the first profile as extra CPU and memory (inspect.py).
        app.state.click_tracker = NoClickTracker()
        cache = app.state.redirect_cache
        # A cache that times out makes every request wait (0.5 s x 2): "hit" and
        # "miss" would both measure the timeout. On Windows, use 127.0.0.1, not
        # localhost (localhost tries IPv6 first, which Docker does not listen on).
        if await cache.ping() is False:
            raise SystemExit(f"Redis does not answer at {settings.redis_url}: fix it first.")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://profile") as client:

            async def empty_cache() -> None:
                await cache.invalidate(short_code)  # the next lookup is a miss

            await measure(client, path, WARM_UP_REQUESTS)  # warm up; fills the cache
            # The "hit" row must really be hits. A recent change (or a run less than
            # 30 s ago) leaves a tombstone that blocks refilling the cache.
            if settings.redis_url and await cache.get(short_code) is None:
                raise SystemExit(
                    f"The cache was not filled for {short_code} (a recent change or profile "
                    "run left a 30 s tombstone): try again in 30 seconds."
                )
            # 1. LATENCY (no profiler running). Hits first: invalidating leaves a
            # 30-second tombstone that blocks refilling the cache.
            hits = await measure(client, path, requests)
            misses = await measure(client, path, requests, before_each=empty_cache)

            # 2. CPU: the miss path does the most work (cache + database)
            profile = cProfile.Profile()
            profile.enable()
            await measure(client, path, requests, before_each=empty_cache)
            profile.disable()

            # 3. MEMORY: allocations while serving `requests` more (after warm-up)
            tracemalloc.start()
            before = tracemalloc.take_snapshot()
            await measure(client, path, requests, before_each=empty_cache)
            after = tracemalloc.take_snapshot()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

    growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    return render_report(
        short_code,
        requests,
        hits,
        misses,
        cpu_table(profile),
        memory_table(before, after),
        growth,
        peak,
        cache_configured=bool(settings.redis_url),
    )


def render_report(
    short_code: str,
    requests: int,
    hits: list[float],
    misses: list[float],
    cpu: str,
    memory: str,
    growth_bytes: int,
    peak_bytes: int,
    cache_configured: bool = True,
) -> str:
    """Build the Markdown report."""
    rows = []
    for label, samples in (("cache hit", hits), ("cache miss", misses)):
        s = latency_summary(samples)
        rows.append(
            f"| {label} | {s['count']} | {s['mean_ms']:.2f} | {s['p50_ms']:.2f} "
            f"| {s['p95_ms']:.2f} | {s['p99_ms']:.2f} | {s['max_ms']:.2f} |"
        )
    return "\n".join(
        [
            f"# Redirect profile: /{short_code}, {requests} requests per pass",
            "",
            "## Latency (ms, in process: no network)",
            "",
            *(
                []
                if cache_configured
                else ["No REDIS_URL: there is no cache, so a hit is also a database read.", ""]
            ),
            "| scenario | requests | mean | p50 | p95 | p99 | max |",
            "|---|---|---|---|---|---|---|",
            *rows,
            "",
            "## CPU (cProfile, cache-miss pass, sorted by own time)",
            "",
            "```text",
            cpu,
            "```",
            "",
            "## Memory (tracemalloc, cache-miss pass)",
            "",
            f"- Peak traced memory: {peak_bytes / 1024:.0f} KiB",
            f"- Memory still held after the pass: {growth_bytes / 1024:.1f} KiB "
            f"= {growth_bytes / requests:.0f} bytes per request",
            "",
            "```text",
            memory,
            "```",
            "",
        ]
    )


async def newest_live_code(settings: Settings) -> str:
    """Return the short code of the newest live URL (to profile when none is given)."""
    from app.db.database import create_engine, create_session_factory

    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            code = await session.scalar(
                select(URL.short_code).where(URL.is_live(utc_now())).order_by(URL.id.desc())
            )
    finally:
        await engine.dispose()
    if code is None:
        raise SystemExit("No live short URL found: create one first (or pass --code).")
    return code


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--code", help="short code to profile (default: the newest live URL)")
    parser.add_argument(
        "--requests", type=int, default=300, help="requests per pass (default: 300)"
    )
    parser.add_argument("--report", help="also write the Markdown report to this file")
    args = parser.parse_args(argv)
    if args.requests < 1:
        parser.error("--requests must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    """Profile the redirect and print (and optionally save) the report."""
    args = parse_args(argv)
    settings = get_settings()
    code = args.code or asyncio.run(newest_live_code(settings))
    report = asyncio.run(profile_redirects(settings, code, args.requests))
    sys.stdout.write(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as file:
            file.write(report)


if __name__ == "__main__":
    main()
