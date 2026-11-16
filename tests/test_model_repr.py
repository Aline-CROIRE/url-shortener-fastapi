"""How models print: handy in logs, the debugger and failing-test messages."""

from datetime import UTC, datetime

from app.models import URL, Click, Tag, User, UserTier


def test_user_repr() -> None:
    user = User(id=1, email="aline@example.com", tier=UserTier.PREMIUM)
    assert repr(user) == "<User id=1 email='aline@example.com' tier=premium>"


def test_url_repr() -> None:
    assert (
        repr(URL(id=7, short_code="py-docs", owner_id=1))
        == "<URL id=7 short_code='py-docs' owner_id=1>"
    )


def test_click_repr() -> None:
    moment = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    assert repr(Click(id=3, url_id=7, clicked_at=moment)) == (
        "<Click id=3 url_id=7 clicked_at=2026-11-12 10:00:00+00:00>"
    )


def test_tag_repr() -> None:
    assert repr(Tag(id=5, name="Ads")) == "<Tag id=5 name='Ads'>"
