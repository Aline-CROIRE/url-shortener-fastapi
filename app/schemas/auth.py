"""Request and response shapes for registering, logging in and refreshing tokens.

Login itself uses FastAPI's OAuth2PasswordRequestForm (a form, email in "username"),
so Swagger's "Authorize" button works; it needs no schema here.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.models.user import UserTier
from app.schemas.base import ORMModel
from app.schemas.user import USERNAME_PATTERN

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128  # an upper limit stops huge passwords from slowing Argon2 down
# A JWT is a few hundred characters; anything far longer is not one of ours
TOKEN_MAX_LENGTH = 2048


class LoginRequest(BaseModel):
    """The email and password of an existing account (cookie login; removed in Step 7)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(examples=["aline@example.com"])
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, email: str) -> str:
        """Lower-case the email, as it was stored at registration."""
        return email.lower()


class UserRegister(BaseModel):
    """What a client sends to POST /auth/register/.

    extra="forbid": any other field (like "tier" or "is_staff") is a 422 error, so
    nobody can register themselves as Premium or Admin.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(examples=["aline@example.com"])
    # SecretStr prints as '**********', so the password never leaks into logs
    password: SecretStr = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        examples=["a-long-passphrase"],
    )
    # Optional: when missing, the server makes one from the email ("aline" from aline@...)
    username: str | None = Field(
        default=None, min_length=3, max_length=150, pattern=USERNAME_PATTERN, examples=["aline"]
    )

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, email: str) -> str:
        """Return the email in lowercase: "Aline@X.com" and "aline@x.com" are one account."""
        return email.lower()


class UserOut(ORMModel):
    """The new account as register returns it: no password hash, no personal data."""

    id: int
    email: EmailStr
    tier: UserTier


class TokenPair(BaseModel):
    """What login and refresh return (the OAuth2 "bearer" token response)."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"


class RefreshIn(BaseModel):
    """What a client sends to POST /auth/refresh/ to get new tokens."""

    model_config = ConfigDict(extra="forbid")

    # SecretStr: the token is a password-like secret, so it is hidden when printed
    refresh_token: SecretStr = Field(min_length=1, max_length=TOKEN_MAX_LENGTH)
