"""Password hashing (Argon2id via pwdlib) and JWT access/refresh tokens (PyJWT, HS256)."""

import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import Settings
from app.core.exceptions import InvalidTokenError

# ---------- passwords ----------

# Argon2id with the library's recommended settings (64 MB memory, 3 passes): slow on
# purpose, so guessing passwords from a stolen database takes far too long.
# A new random salt is made for every hash and stored inside the hash string.
_password_hash = PasswordHash((Argon2Hasher(),))

SESSION_COOKIE = "session"  # the cookie the browser sends back after login
SESSION_USER_KEY = "user_id"  # what the signed session cookie stores

# A real hash of a random password nobody knows: when the email is unknown, login
# still runs one verification against it, so the response time does not reveal
# which emails are registered
DUMMY_HASH = _password_hash.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    """Return the Argon2id hash of a password (the only form that is stored)."""
    return _password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Return True if the password matches the stored hash.

    A wrong password returns False. A value that is not a known hash (like "!"
    for accounts that cannot log in) also returns False instead of raising.
    """
    try:
        return _password_hash.verify(password, hashed_password)
    except UnknownHashError:
        return False


# ---------- JWT ----------

# Fixed by the SERVER, never read from the token's header: blocks "alg: none"
# and algorithm-swap attacks
JWT_ALGORITHM = "HS256"
# Every token must carry all of these, or it is rejected
REQUIRED_CLAIMS = ["sub", "type", "iat", "exp", "jti"]


class TokenType(StrEnum):
    """What a token may be used for (the custom "type" claim)."""

    ACCESS = "access"  # sent with every request, short-lived
    REFRESH = "refresh"  # only swapped for a new access token, long-lived


def _create_token(user_id: int, token_type: TokenType, lifetime: timedelta, key: str) -> str:
    """Sign a token with only the five claims we need (no personal data)."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),  # the JWT standard wants the subject as a string
        "type": token_type.value,
        "iat": now,  # PyJWT turns datetimes into Unix timestamps
        "exp": now + lifetime,
        "jti": secrets.token_urlsafe(16),  # unique id: lets a future blocklist revoke it
    }
    return jwt.encode(payload, key, algorithm=JWT_ALGORITHM)


def create_access_token(user_id: int, settings: Settings) -> str:
    """Return a short-lived access token (ACCESS_TOKEN_MINUTES, 15 by default)."""
    lifetime = timedelta(minutes=settings.access_token_minutes)
    key = settings.jwt_secret_key.get_secret_value()
    return _create_token(user_id, TokenType.ACCESS, lifetime, key)


def create_refresh_token(user_id: int, settings: Settings) -> str:
    """Return a long-lived refresh token (REFRESH_TOKEN_DAYS, 7 by default)."""
    lifetime = timedelta(days=settings.refresh_token_days)
    key = settings.jwt_secret_key.get_secret_value()
    return _create_token(user_id, TokenType.REFRESH, lifetime, key)


def decode_token(token: str, expected_type: TokenType, settings: Settings) -> int:
    """Check a token and return the user id it was issued for.

    Checks: the signature (HS256 only), not expired, every claim present, the
    expected type (an access token is refused where a refresh token is needed,
    and the other way round) and a numeric user id.

    Raises:
        InvalidTokenError: For ANY failure. It always fails closed (401).
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[JWT_ALGORITHM],  # the server's list, not the token's "alg"
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.InvalidTokenError as exc:  # bad signature, expired, missing claim, garbage...
        raise InvalidTokenError() from exc

    subject = payload["sub"]
    if payload["type"] != expected_type.value or not (subject.isascii() and subject.isdigit()):
        raise InvalidTokenError()
    return int(subject)
