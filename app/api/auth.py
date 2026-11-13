"""Who is logged in: read the signed session cookie and load the user.

After POST /api/v1/auth/login/ the browser keeps a signed "session" cookie that
holds the user id and sends it back with every request. Every endpoint that
uses CurrentUser or CurrentOwnerId therefore works only for a logged-in user,
and the services filter all data by that user's id.
"""

from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import NotAuthenticatedError
from app.core.security import SESSION_COOKIE, SESSION_USER_KEY
from app.models import User

# Documents the cookie in Swagger (the lock icons). The cookie itself is checked
# and decoded by Starlette's SessionMiddleware; request.session holds its data.
session_cookie = APIKeyCookie(
    name=SESSION_COOKIE,
    auto_error=False,
    description="Set by POST /api/v1/auth/login/; the browser sends it automatically.",
)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the factory for short database sessions (tests replace it)."""
    return request.app.state.session_factory


async def get_current_user(
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    _cookie: Annotated[str | None, Security(session_cookie)] = None,
) -> User:
    """Return the logged-in user, or raise 401.

    It uses its OWN short database session (not the request's), so the services
    can still open their own transaction with session.begin().

    Raises:
        NotAuthenticatedError: No valid login cookie, or the user was deleted or
            switched off.
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if not isinstance(user_id, int):
        raise NotAuthenticatedError()
    async with session_factory() as session:
        user = await session.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()  # forget a login that is no longer valid
        raise NotAuthenticatedError()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_owner_id(user: CurrentUser) -> int:
    """Return the logged-in user's id: every URL query is filtered by it."""
    return user.id


CurrentOwnerId = Annotated[int, Depends(get_current_owner_id)]
