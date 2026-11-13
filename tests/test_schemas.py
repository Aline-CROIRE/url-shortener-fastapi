from datetime import UTC, date, datetime, timedelta
from ipaddress import IPv4Address

import pytest
from pydantic import ValidationError
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import URL, Click, Tag, User, UserTier
from app.schemas.analytics import CountryCount, DailyCount, URLAnalytics
from app.schemas.click import ClickRead
from app.schemas.tag import TagRead
from app.schemas.url import URLCreate, URLRead, URLUpdate
from app.schemas.user import UserCreate, UserRead

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
BASE_URL = "http://short.test/"


def make_user() -> User:
    return User(
        id=1,
        email="aline@example.com",
        username="aline",
        first_name="Aline",
        last_name="Tester",
        hashed_password="secret-hash",
        is_active=True,
        tier=UserTier.FREE,  # also sets is_premium=False
        created_at=NOW,
        updated_at=NOW,
    )


def make_url() -> URL:
    # Plain Python objects: no database needed to test the JSON shape
    return URL(
        id=7,
        original_url="https://www.python.org/",
        short_code="py-docs",
        custom_alias="py-docs",
        is_active=True,
        expires_at=None,
        click_count=3,
        created_at=NOW,
        updated_at=NOW,
        owner=make_user(),
        tags=[Tag(id=1, name="Marketing"), Tag(id=2, name="Social")],
    )


def error_fields(error: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return {str(item["loc"][0]) for item in error.value.errors()}


# ---------- users ----------


def test_user_create_lowercases_email() -> None:
    user = UserCreate(email="Aline@Example.COM", username="aline", password="long-enough")
    assert user.email == "aline@example.com"


def test_user_create_rejects_bad_input() -> None:
    with pytest.raises(ValidationError) as error:
        UserCreate(email="not-an-email", username="a!", password="short")
    assert error_fields(error) == {"email", "username", "password"}


def test_password_is_hidden_when_printed() -> None:
    user = UserCreate(email="a@example.com", username="aline", password="super-secret")
    assert "super-secret" not in repr(user)
    assert user.password.get_secret_value() == "super-secret"


def test_user_read_never_shows_the_password_hash() -> None:
    data = UserRead.model_validate(make_user()).model_dump()

    assert "hashed_password" not in data
    assert data["tier"] == "free"
    assert (data["first_name"], data["is_active"], data["is_premium"]) == ("Aline", True, False)
    assert data["email"] == "aline@example.com"


# ---------- URL input ----------


def test_url_create_minimal() -> None:
    url = URLCreate(original_url="https://www.python.org/")
    assert (url.custom_alias, url.expires_at, url.tags) == (None, None, [])


def test_url_create_cleans_tags() -> None:
    url = URLCreate(original_url="https://x.com/", tags=["  Ads ", "ads", "Social"])
    assert url.tags == ["Ads", "Social"]


@pytest.mark.parametrize(
    "alias",
    ["ab", "way-too-long-alias", "has space", "emoji😀", "docs", "HEALTH"],
)
def test_url_create_rejects_bad_aliases(alias: str) -> None:
    with pytest.raises(ValidationError) as error:
        URLCreate(original_url="https://x.com/", custom_alias=alias)
    assert error_fields(error) == {"custom_alias"}


@pytest.mark.parametrize(
    "expires_at",
    [
        datetime.now(UTC) - timedelta(minutes=1),  # in the past
        datetime(2099, 1, 1),  # no timezone
    ],
)
def test_url_create_rejects_bad_expiry(expires_at: datetime) -> None:
    with pytest.raises(ValidationError) as error:
        URLCreate(original_url="https://x.com/", expires_at=expires_at)
    assert error_fields(error) == {"expires_at"}


def test_url_create_rejects_bad_tags_and_unknown_fields() -> None:
    with pytest.raises(ValidationError) as error:
        URLCreate(original_url="https://x.com/", tags=["   "], short_code="mine")
    assert error_fields(error) == {"tags", "short_code"}


def test_url_create_allows_at_most_10_tags() -> None:
    with pytest.raises(ValidationError):
        URLCreate(original_url="https://x.com/", tags=[f"tag{i}" for i in range(11)])


def test_url_update_keeps_only_sent_fields() -> None:
    update = URLUpdate(title="Python home", expires_at=None)

    # expires_at=None was SENT (remove the expiry); the other fields were not
    assert update.model_dump(exclude_unset=True) == {"title": "Python home", "expires_at": None}


@pytest.mark.parametrize("field", ["original_url", "is_active", "tags"])
def test_url_update_refuses_null_for_required_columns(field: str) -> None:
    with pytest.raises(ValidationError) as error:
        URLUpdate(**{field: None})
    assert error_fields(error) == {field}


def test_url_update_cannot_change_the_short_code() -> None:
    with pytest.raises(ValidationError) as error:
        URLUpdate(short_code="new")
    assert error_fields(error) == {"short_code"}


# ---------- URL output ----------


def test_url_read_from_model_with_owner_and_tags() -> None:
    data = URLRead.model_validate(make_url(), context={"base_url": BASE_URL}).model_dump()

    assert data["short_url"] == "http://short.test/py-docs"
    assert data["owner"] == {"id": 1, "username": "aline"}  # no email, no password
    assert data["tags"] == [{"id": 1, "name": "Marketing"}, {"id": 2, "name": "Social"}]
    assert data["click_count"] == 3


def test_url_read_needs_the_base_url() -> None:
    with pytest.raises(ValidationError, match="base_url"):
        URLRead.model_validate(make_url())


def test_tag_read_from_model() -> None:
    assert TagRead.model_validate(Tag(id=5, name="Ads")).model_dump() == {"id": 5, "name": "Ads"}


# ---------- clicks and analytics ----------


def test_click_read_turns_ip_text_into_an_ip_object() -> None:
    click = Click(id=1, url_id=7, clicked_at=NOW, ip_address="41.186.0.10", country="RW")

    read = ClickRead.model_validate(click)

    assert read.ip_address == IPv4Address("41.186.0.10")
    assert read.model_dump(mode="json")["ip_address"] == "41.186.0.10"  # text in JSON


@pytest.mark.anyio
async def test_analytics_schemas_read_sql_rows(db_session: AsyncSession) -> None:
    # A real SQL row whose columns are named with .label(), like F2.5 will produce
    row = (
        await db_session.execute(select(literal("RW").label("country"), literal(4).label("clicks")))
    ).one()

    assert CountryCount.model_validate(row) == CountryCount(country="RW", clicks=4)


def test_url_analytics_shape() -> None:
    analytics = URLAnalytics(
        short_code="py-docs",
        days=30,
        total_clicks=4,
        unique_visitors=2,
        first_click_at=None,
        last_click_at=None,
        clicks_per_country=[CountryCount(country="RW", clicks=4)],
        clicks_per_city=[],
        clicks_per_day=[DailyCount(day=date(2026, 11, 12), clicks=4)],
        top_referrers=[],
    )

    assert analytics.model_dump(mode="json")["clicks_per_day"] == [
        {"day": "2026-11-12", "clicks": 4}
    ]
