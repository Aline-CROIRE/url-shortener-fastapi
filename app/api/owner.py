"""TEMPORARY owner mechanism until real authentication (JWT) in Module 7.

TODO(module-7): replace get_current_owner_id() with "decode the JWT from the
Authorization header and return its user id". It already returns a plain user
id, so the routes and services that use CurrentOwnerId will not change.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models import User

DEMO_OWNER_EMAIL = "demo@url-shortener.local"
DEMO_OWNER_USERNAME = "demo"
# "!" is not a valid hash of any password, so nobody can ever log in as this user
UNUSABLE_PASSWORD_HASH = "!"


async def get_current_owner_id(request: Request) -> int:
    """Return the id of the one demo user that owns every URL for now.

    The user is created once. It uses its OWN short session, not the request's
    session, so the services can still open their transaction with session.begin().
    """
    cached: int | None = getattr(request.app.state, "demo_owner_id", None)
    if cached is not None:
        return cached

    async with request.app.state.session_factory() as session, session.begin():
        # Safe even if two first requests arrive at the same moment
        await session.execute(
            insert(User)
            .values(
                email=DEMO_OWNER_EMAIL,
                username=DEMO_OWNER_USERNAME,
                hashed_password=UNUSABLE_PASSWORD_HASH,
            )
            .on_conflict_do_nothing()
        )
        owner_id = await session.scalar(select(User.id).where(User.email == DEMO_OWNER_EMAIL))

    request.app.state.demo_owner_id = owner_id
    return owner_id


CurrentOwnerId = Annotated[int, Depends(get_current_owner_id)]
