"""Who is calling: read the Bearer access token and load the user.

After POST /api/v1/auth/login/ the client holds an access token and sends it with
every request:  Authorization: Bearer <access token>
Every endpoint that uses CurrentUser works only with a valid one. What the
user may then do is checked by tier (app/api/permissions.py) and by ownership
(app/services/urls.py).
"""

from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import SettingsDep
from app.core.exceptions import InvalidTokenError, NotAuthenticatedError
from app.core.security import TokenType, decode_token
from app.models import User

# Reads "Authorization: Bearer <token>" and shows the "Authorize" button in Swagger,
# which logs in through tokenUrl. auto_error=False: a missing token raises OUR
# error (same JSON shape as every other error) instead of FastAPI's default one.
# Only the header is read: a token in the URL would end up in logs and history.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/login/",
    auto_error=False,
    description="Log in with your EMAIL in the 'username' field.",
)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the factory for short database sessions (tests replace it)."""
    return request.app.state.session_factory


async def get_current_user(
    token: Annotated[str | None, Security(oauth2_scheme)],
    settings: SettingsDep,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> User:
    """Return the user of a valid access token, or raise 401.

    The token only says WHO the user is. Everything else (tier, is_active) is read
    from the database on every request, so a promotion, a switch-off or a deletion
    counts at once, not when the token expires.

    It uses its OWN short database session (not the request's), so the services
    can still open their own transaction with session.begin().

    Raises:
        NotAuthenticatedError: No token was sent.
        InvalidTokenError: The token is bad, expired or a refresh token, or its
            user was deleted or switched off.
    """
    if not token:
        raise NotAuthenticatedError()
    user_id = decode_token(token, TokenType.ACCESS, settings)
    async with session_factory() as session:
        user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise InvalidTokenError()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
