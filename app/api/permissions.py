"""Tier checks (role-based access control) as FastAPI dependencies.

Two kinds of "may you do this?" exist in this app:
- by TIER (here): "is your account Premium?" It needs only the user.
- by OWNERSHIP (in the services): "is this URL yours?" It needs the URL too, so
  it is checked where the URL is loaded (app/services/urls.py).

Usage:
    @router.get("/premium-thing")
    async def premium_thing(user: PremiumUser) -> ...

The user comes from get_current_user, which reads the tier from the database on
every request, so a promotion or a downgrade counts at once.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends

from app.api.auth import CurrentUser
from app.core.exceptions import InsufficientTierError
from app.models import User, UserTier


def require_tier(*allowed: UserTier) -> Callable[[User], Awaitable[User]]:
    """Return a dependency that lets only users of the given tiers through.

    Raises:
        ValueError: If no tier is given (a programming mistake, found at startup).
    """
    if not allowed:
        raise ValueError("require_tier needs at least one tier")
    allowed_tiers = frozenset(allowed)
    names = " or ".join(tier.value.capitalize() for tier in allowed)  # "Premium or Admin"

    async def check_tier(user: CurrentUser) -> User:
        """Return the user if their tier is allowed.

        Raises:
            InsufficientTierError: The user's tier is not one of the allowed ones (403).
        """
        if user.tier not in allowed_tiers:
            raise InsufficientTierError(f"This needs a {names} account.")
        return user

    return check_tier


# Premium features are open to Premium AND Admin users
require_premium = require_tier(UserTier.PREMIUM, UserTier.ADMIN)

PremiumUser = Annotated[User, Depends(require_premium)]


def is_admin(user: User) -> bool:
    """Return True if the user may manage every URL (the Admin override)."""
    return user.tier == UserTier.ADMIN
