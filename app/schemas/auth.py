"""Request and response shapes for registering, logging in and refreshing tokens.

Login itself uses FastAPI's OAuth2PasswordRequestForm (a form, email in "username"),
so Swagger's "Authorize" button works; it needs no schema here.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.schemas.user import USERNAME_MAX_LENGTH, USERNAME_MIN_LENGTH, USERNAME_PATTERN

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128  # an upper limit stops huge passwords from slowing Argon2 down
# A JWT is a few hundred characters; anything far longer is not one of ours
TOKEN_MAX_LENGTH = 2048
# RFC 5321: an email address is at most 254 characters
EMAIL_MAX_LENGTH = 254


class UserRegister(BaseModel):
    """What a client sends to POST /auth/register/.

    extra="forbid": any other field (like "tier" or "is_active") is a 422 error, so
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
        default=None,
        min_length=USERNAME_MIN_LENGTH,
        max_length=USERNAME_MAX_LENGTH,
        pattern=USERNAME_PATTERN,
        examples=["aline"],
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, email: str) -> str:
        """Accept only ASCII emails and return them in lowercase.

        Lowercase: "Aline@X.com" and "aline@x.com" are one account.
        ASCII only: a look-alike letter (Cyrillic U+0430 looks exactly like "a")
        would otherwise create a second account that looks identical to someone
        else's. Spaces around the email are already removed by EmailStr.
        """
        if not email.isascii():
            raise ValueError("Use an email address with only ASCII letters, digits and symbols.")
        return email.lower()

    @field_validator("password")
    @classmethod
    def refuse_blank_password(cls, password: SecretStr) -> SecretStr:
        """Refuse a password made only of spaces, tabs or new lines."""
        if not password.get_secret_value().strip():
            raise ValueError("The password cannot be only spaces.")
        return password


# The ONE answer register gives, whether the email was new or already taken
REGISTER_MESSAGE = (
    "If this email can be used, the account is ready: log in with your email and password."
)


class RegisterAccepted(BaseModel):
    """What POST /auth/register/ answers, the same for every email (202).

    No id, email or tier: those would show whether the email was already
    registered. After logging in, GET /auth/me/ shows the account.
    """

    message: str = REGISTER_MESSAGE


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
