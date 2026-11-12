from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, SecretStr, field_validator

from app.models.user import UserTier
from app.schemas.base import ORMModel

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]+$"


class UserCreate(BaseModel):
    """What a client sends to create a user (registration arrives in Module 7)."""

    email: EmailStr = Field(examples=["aline@example.com"])
    username: str = Field(min_length=3, max_length=50, pattern=USERNAME_PATTERN, examples=["aline"])
    # SecretStr prints as '**********', so the password never leaks into logs.
    # It is hashed in Module 7; only the hash is stored (users.hashed_password).
    password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, email: str) -> str:
        # "Aline@Example.com" and "aline@example.com" are the same person
        return email.lower()


class UserRead(ORMModel):
    """A user as the API shows it. No password hash, ever."""

    id: int
    email: EmailStr
    username: str
    is_premium: bool
    tier: UserTier
    created_at: datetime


class OwnerRead(ORMModel):
    """The small owner shown inside a URL: no email or tier."""

    id: int
    username: str
