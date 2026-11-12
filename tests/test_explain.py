"""The EXPLAIN report runs on the real statements and our indexes can be used.

With tiny test data Postgres may correctly prefer a full scan (it is cheaper for
a few rows), so the index test switches sequential scans off to ask:
"CAN this query use our index?" The live run on seeded data shows the real choice.
"""

import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.scripts import explain as explain_module
from app.scripts.explain import NO_DATA_MESSAGE, ExplainedQuery, explain_report, print_report
from app.scripts.seed import SeedCounts, seed_database

RunDb = Callable[..., Any]


def report_on_seeded_data(run_db: RunDb, *, no_seq_scans: bool) -> list[ExplainedQuery]:
    async def work(session: AsyncSession) -> list[ExplainedQuery]:
        connection = await session.connection()
        await seed_database(
            connection,
            SeedCounts(users=3, urls=60, clicks=600),
            random.Random(1),
            now=datetime.now(UTC),
        )
        if no_seq_scans:
            await connection.execute(text("SET LOCAL enable_seqscan = off"))
        return await explain_report(connection)

    return run_db(work)


def test_report_explains_every_important_query(run_db: RunDb) -> None:
    report = report_on_seeded_data(run_db, no_seq_scans=False)

    assert len(report) == 8
    for item in report:
        assert any(line.startswith("Execution Time") for line in item.plan), item.title


def test_our_indexes_can_be_used(run_db: RunDb) -> None:
    report = {item.title: item for item in report_on_seeded_data(run_db, no_seq_scans=True)}

    def indexes(title_start: str) -> list[str]:
        return next(v.indexes_used for k, v in report.items() if k.startswith(title_start))

    assert "ix_urls_short_code" in indexes("Redirect")
    assert "ix_urls_owner_id_created_at_id" in indexes("URL list: first page")
    assert "ix_urls_owner_id_created_at_id" in indexes("URL list: page 6")
    assert "ix_clicks_url_id_clicked_at" in indexes("Analytics: clicks per UTC day")
    assert "ix_clicks_url_id_clicked_at" in indexes("Analytics: summary")


def test_explained_query_reads_index_names_and_seq_scans() -> None:
    item = ExplainedQuery(
        "example",
        [
            "Index Scan Backward using ix_a on urls",
            "  ->  Bitmap Index Scan on ix_b",
            "  ->  Seq Scan on users users_1",
        ],
    )

    assert item.indexes_used == ["ix_a", "ix_b"]
    assert item.sequential_scans == ["users"]


# ---------- the command line ----------


def test_command_line_on_an_empty_database_explains_what_to_do(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(explain_module, "get_settings", lambda: test_settings)

    explain_module.main()  # this used to crash with NoResultFound (bug fixed in F3.3)

    assert NO_DATA_MESSAGE in capsys.readouterr().out


def test_print_report_shows_plans_and_a_summary(capsys: pytest.CaptureFixture) -> None:
    print_report(
        [
            ExplainedQuery("Fast lookup", ["Index Scan using ix_urls_short_code on urls"]),
            ExplainedQuery("Slow search", ["Seq Scan on urls"]),
        ]
    )

    out = capsys.readouterr().out
    assert "Fast lookup" in out and "--> indexes used: ix_urls_short_code" in out
    assert "SEQ SCAN on: urls" in out
