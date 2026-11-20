"""Password hashing (Argon2id via pwdlib) and JWT access/refresh tokens (PyJWT, HS256)."""

import secrets
import unicodedata
from dataclasses import dataclass
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

# A real hash of a random password nobody knows: when the email is unknown, login
# still runs one verification against it, so the response time does not reveal
# which emails are registered
DUMMY_HASH = _password_hash.hash(secrets.token_urlsafe(16))


def _normalize(password: str) -> str:
    """Return the password in Unicode NFKC form (NIST SP 800-63B).

    "é" can arrive as ONE character or as "e" + an accent mark, depending on the
    keyboard. After NFKC both are the same text, so both log in. Letter case and
    spaces are never changed.
    """
    return unicodedata.normalize("NFKC", password)


def hash_password(password: str) -> str:
    """Return the Argon2id hash of a password (the only form that is stored)."""
    return _password_hash.hash(_normalize(password))


def verify_password(password: str, hashed_password: str) -> bool:
    """Return True if the password matches the stored hash.

    A wrong password returns False. A value that is not a known hash (like "!"
    for accounts that cannot log in) also returns False instead of raising, AFTER
    one check against DUMMY_HASH: otherwise such accounts would answer much faster
    than real ones, and the response time would point them out.
    """
    try:
        return _password_hash.verify(_normalize(password), hashed_password)
    except UnknownHashError:
        _password_hash.verify(_normalize(password), DUMMY_HASH)  # same slow work, result unused
        return False


# ---------- JWT ----------

# Fixed by the SERVER, never read from the token's header: blocks "alg: none"
# and algorithm-swap attacks
JWT_ALGORITHM = "HS256"
# Every token must carry all of these, or it is rejected
REQUIRED_CLAIMS = ["sub", "type", "iat", "exp", "jti"]
# Server clocks can differ by a few seconds: tolerate that much on iat and exp,
# or a token made by one server could be refused by another for a moment
CLOCK_LEEWAY = timedelta(seconds=30)
# users.id is a Postgres INTEGER: no real user id is larger
MAX_USER_ID = 2**31 - 1


class TokenType(StrEnum):
    """What a token may be used for (the custom "type" claim)."""

    ACCESS = "access"  # sent with every request, short-lived
    REFRESH = "refresh"  # only swapped for a new access token, long-lived


@dataclass(frozen=True)
class IssuedToken:
    """A signed token with the id (jti) and expiry the server may need to store."""

    token: str
    jti: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenClaims:
    """What a checked token says: whose it is, its id, and when it was issued."""

    user_id: int
    jti: str
    issued_at: datetime


def is_revoked(issued_at: datetime, tokens_valid_after: datetime | None) -> bool:
    """Return True if the token was issued before the user's tokens were revoked.

    Both times are exact (fractions of a second) and come from the app's clock, so
    a token issued a moment before the revocation is refused, and a new login a
    moment after it works, even within the same second.
    """
    return tokens_valid_after is not None and issued_at < tokens_valid_after


def _create_token(
    user_id: int, token_type: TokenType, lifetime: timedelta, key: str
) -> IssuedToken:
    """Sign a token with only the five claims we need (no personal data)."""
    now = datetime.now(UTC)
    jti = secrets.token_urlsafe(16)  # unique id: refresh tokens are stored by it
    payload = {
        "sub": str(user_id),  # the JWT standard wants the subject as a string
        "type": token_type.value,
        # The EXACT issue time, with fractions of a second (allowed by RFC 7519), so a
        # revocation can refuse every token issued before it, even in the same second
        "iat": now.timestamp(),
        "exp": now + lifetime,  # PyJWT turns the datetime into whole Unix seconds
        "jti": jti,
    }
    token = jwt.encode(payload, key, algorithm=JWT_ALGORITHM)
    return IssuedToken(token=token, jti=jti, expires_at=now + lifetime)


def create_access_token(user_id: int, settings: Settings) -> str:
    """Return a short-lived access token (ACCESS_TOKEN_MINUTES, 15 by default)."""
    lifetime = timedelta(minutes=settings.access_token_minutes)
    key = settings.jwt_secret_key.get_secret_value()
    return _create_token(user_id, TokenType.ACCESS, lifetime, key).token


def issue_refresh_token(user_id: int, settings: Settings) -> IssuedToken:
    """Return a long-lived refresh token (REFRESH_TOKEN_DAYS) with its jti and expiry."""
    lifetime = timedelta(days=settings.refresh_token_days)
    key = settings.jwt_secret_key.get_secret_value()
    return _create_token(user_id, TokenType.REFRESH, lifetime, key)


def create_refresh_token(user_id: int, settings: Settings) -> str:
    """Return a long-lived refresh token (REFRESH_TOKEN_DAYS, 7 by default)."""
    return issue_refresh_token(user_id, settings).token


def _valid_user_id(subject: object) -> int | None:
    """Return the user id in "sub", or None if it is not a plain, possible id.

    Plain ASCII digits only ("١٢" or "-1" are refused), no leading zero ("007"),
    and within the users.id column: a strange value must give 401, never a 500
    from the database.
    """
    if not isinstance(subject, str) or not (subject.isascii() and subject.isdigit()):
        return None
    if len(subject) > len(str(MAX_USER_ID)) or subject.startswith("0"):
        return None
    user_id = int(subject)
    return user_id if 1 <= user_id <= MAX_USER_ID else None


def decode_token_claims(token: str, expected_type: TokenType, settings: Settings) -> TokenClaims:
    """Check a token and return its user id and jti.

    Checks: the signature (HS256 only), not expired, every claim present, the
    expected type (an access token is refused where a refresh token is needed,
    and the other way round) and a valid user id. Clocks may differ by up to
    CLOCK_LEEWAY.

    Raises:
        InvalidTokenError: For ANY failure. It always fails closed (401).
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[JWT_ALGORITHM],  # the server's list, not the token's "alg"
            options={"require": REQUIRED_CLAIMS},
            leeway=CLOCK_LEEWAY,
        )
    except jwt.InvalidTokenError as exc:  # bad signature, expired, missing claim, garbage...
        raise InvalidTokenError() from exc

    user_id = _valid_user_id(payload["sub"])
    jti = payload["jti"]
    if payload["type"] != expected_type.value or user_id is None or not isinstance(jti, str):
        raise InvalidTokenError()
    # PyJWT has already checked that iat is a number: a Unix time, maybe with fractions
    issued_at = datetime.fromtimestamp(float(payload["iat"]), tz=UTC)
    return TokenClaims(user_id=user_id, jti=jti, issued_at=issued_at)


def decode_token(token: str, expected_type: TokenType, settings: Settings) -> int:
    """Check a token (see decode_token_claims) and return the user id it was issued for.

    Raises:
        InvalidTokenError: For ANY failure. It always fails closed (401).
    """
    return decode_token_claims(token, expected_type, settings).user_id
