"""The seed script: runs inside the test transaction (rolled back afterwards)."""

import asyncio
import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.database import create_engine
from app.models import URL, Click, User, url_tags
from app.scripts import seed as seed_module
from app.scripts.seed import SEED_EMAIL_DOMAIN, SeedCounts, reset_seeded_data, seed_database

RunDb = Callable[..., Any]
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
SMALL = SeedCounts(users=3, urls=40, clicks=500)
SEEDED_USER = User.email.like(f"%@{SEED_EMAIL_DOMAIN}")


def run_seed(run_db: RunDb, counts: SeedCounts = SMALL, seed: int = 42) -> Any:
    async def work(session: AsyncSession) -> Any:
        connection = await session.connection()  # the test's own (rolled back) connection
        return await seed_database(connection, counts, random.Random(seed), now=NOW)

    return run_db(work)


def query(run_db: RunDb, sql: str) -> Any:
    async def work(session: AsyncSession) -> Any:
        return (await session.execute(text(sql))).scalar_one()

    return run_db(work)


def snapshot(run_db: RunDb) -> list[tuple[str, int]]:
    async def work(session: AsyncSession) -> list[tuple[str, int]]:
        rows = await session.execute(
            select(URL.short_code, URL.click_count)
            .join(URL.owner)
            .where(SEEDED_USER)
            .order_by(URL.short_code)
        )
        return [tuple(row) for row in rows]

    return run_db(work)


def test_seed_inserts_the_requested_amounts(run_db: RunDb) -> None:
    report = run_seed(run_db)

    assert (report.users, report.urls, report.clicks) == (3, 40, 500)
    assert query(run_db, f"SELECT count(*) FROM users WHERE email LIKE '%@{SEED_EMAIL_DOMAIN}'") == 3
    assert query(run_db, "SELECT count(*) FROM clicks") == 500
    assert query(run_db, "SELECT count(*) FROM url_tags") == report.url_tag_links


def test_click_count_equals_the_real_number_of_clicks(run_db: RunDb) -> None:
    run_seed(run_db)

    mismatches = query(
        run_db,
        "SELECT count(*) FROM urls u "
        "WHERE u.click_count <> (SELECT count(*) FROM clicks c WHERE c.url_id = u.id)",
    )
    assert mismatches == 0


def test_clicks_never_happen_before_their_url_existed(run_db: RunDb) -> None:
    run_seed(run_db)

    impossible = query(
        run_db,
        "SELECT count(*) FROM clicks c JOIN urls u ON u.id = c.url_id WHERE c.clicked_at < u.created_at",
    )
    assert impossible == 0


def test_same_seed_gives_the_same_data(run_db: RunDb) -> None:
    run_seed(run_db, seed=7)
    first = snapshot(run_db)

    async def reset(session: AsyncSession) -> None:
        await reset_seeded_data(await session.connection())

    run_db(reset)
    run_seed(run_db, seed=7)

    assert snapshot(run_db) == first


def test_seeding_twice_without_reset_is_refused(run_db: RunDb) -> None:
    run_seed(run_db)

    with pytest.raises(RuntimeError, match="--reset"):
        run_seed(run_db)


def test_reset_removes_only_seeded_data(run_db: RunDb) -> None:
    async def add_real_user(session: AsyncSession) -> None:
        session.add(User(email="real@example.com", username="real", hashed_password="h"))

    run_db(add_real_user)
    run_seed(run_db)

    async def reset(session: AsyncSession) -> int:
        return await reset_seeded_data(await session.connection())

    assert run_db(reset) == 3
    assert query(run_db, "SELECT count(*) FROM users WHERE email = 'real@example.com'") == 1
    assert query(run_db, "SELECT count(*) FROM clicks") == 0  # CASCADE removed them
    assert query(run_db, "SELECT count(*) FROM url_tags") == 0


def test_refuses_to_run_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_module, "get_settings", lambda: Settings(environment="production"))

    with pytest.raises(SystemExit, match="production"):
        seed_module.main([])


@pytest.mark.parametrize("args", [["--urls", "0"], ["--users", "0"], ["--clicks", "-1"]])
def test_rejects_impossible_amounts(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        seed_module.main(args)


def test_seeded_data_uses_url_tags_and_clicks(run_db: RunDb) -> None:
    run_seed(run_db)

    async def work(session: AsyncSession) -> tuple[int, int]:
        links = await session.scalar(select(func.count()).select_from(url_tags))
        clicks = await session.scalar(select(func.count()).select_from(Click))
        return links, clicks

    links, clicks = run_db(work)
    assert links > 0 and clicks == 500


# ---------- the random-code clash loop and the real command line ----------


class RepeatFirstCode(random.Random):
    """A random generator whose first two short codes are the same: forces a clash."""

    def __init__(self, seed: int) -> None:
        super().__init__(seed)
        self.codes_made = 0

    def choices(self, population: Any, weights: Any = None, *, cum_weights: Any = None, k: int = 1) -> list:
        if population is seed_module.CODE_ALPHABET:
            self.codes_made += 1
            if self.codes_made <= 2:
                return list("SameCod")
        return super().choices(population, weights, cum_weights=cum_weights, k=k)


def test_a_repeated_random_code_is_replaced(run_db: RunDb) -> None:
    async def work(session: AsyncSession) -> list[str]:
        rng = RepeatFirstCode(1)
        await seed_database(await session.connection(), SeedCounts(users=1, urls=2, clicks=0), rng, now=NOW)
        return list(await session.scalars(select(URL.short_code).order_by(URL.id)))

    codes = run_db(work)

    assert codes[0] == "SameCod"
    assert codes[1] != "SameCod"  # the clash was noticed and a new code was drawn


def test_command_line_seeds_and_resets(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # The real CLI commits, so it runs against the TEST database and cleans up after
    monkeypatch.setattr(seed_module, "get_settings", lambda: test_settings)
    small = ["--users", "2", "--urls", "3", "--clicks", "10"]
    try:
        seed_module.main(small)
        assert "Seeded 2 users, 3 URLs" in capsys.readouterr().out

        seed_module.main([*small, "--reset"])
        assert "Removed 2 seeded users" in capsys.readouterr().out
    finally:
        cleanup = create_engine(test_settings.database_url)

        async def remove_seed() -> None:
            async with cleanup.begin() as connection:
                await reset_seeded_data(connection)
            await cleanup.dispose()

        asyncio.run(remove_seed())
