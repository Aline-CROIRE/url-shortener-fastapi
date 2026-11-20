"""Output schemas for users (registration input is UserRegister in app/schemas/auth.py)."""

from datetime import datetime

from pydantic import EmailStr

from app.models.user import UserTier
from app.schemas.base import ORMModel

# Letters, digits, "_", "." and "-" only: safe to show anywhere, no look-alike symbols
USERNAME_PATTERN = r"^[A-Za-z0-9_.-]+$"
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 150  # the size of the users.username column


class UserRead(ORMModel):
    """A user as the API shows it. No password hash, ever."""

    id: int
    email: EmailStr
    username: str
    is_active: bool
    is_premium: bool
    tier: UserTier
    created_at: datetime


class OwnerRead(ORMModel):
    """The small owner shown inside a URL: no email or tier."""

    id: int
    username: str
