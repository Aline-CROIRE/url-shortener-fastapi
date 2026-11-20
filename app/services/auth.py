"""The auth service: register a user, log in, and swap a refresh token for new tokens."""

import asyncio
import re
import secrets
from collections.abc import Iterator

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, InvalidCredentialsError, InvalidTokenError
from app.core.security import (
    DUMMY_HASH,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.errors import unique_violation_constraint
from app.models import User
from app.repositories import UserRepository
from app.schemas.auth import EMAIL_MAX_LENGTH, PASSWORD_MAX_LENGTH, TokenPair, UserRegister
from app.schemas.user import USERNAME_MIN_LENGTH

EMAIL_TAKEN = ("email", "This email is already registered.")
USERNAME_TAKEN = ("username", "This username is already taken.")
TAKEN_MESSAGES = {"uq_users_email": EMAIL_TAKEN, "uq_users_username": USERNAME_TAKEN}

# A made username: the start of the email, then a random number if it is taken
USERNAME_ATTEMPTS = 5  # the plain name + 4 numbered tries, then ask the user to choose
USERNAME_BASE_MAX_LENGTH = 140  # leaves room for the number (column holds 150)
NOT_USERNAME_CHARACTERS = re.compile(r"[^a-z0-9_.-]")


def username_suffix() -> str:
    """Return a random number (0-9999) to add to a username that is taken.

    Random, not "aline2, aline3...": counting up would need one query per taken
    name, and two people registering at once would still pick the same number.
    """
    return str(secrets.randbelow(10_000))


def username_base(email: str) -> str:
    """Make a valid username from the part of the email before the "@".

    Keeps only a-z, 0-9, "_", "." and "-" ("a+promo" -> "apromo"); a result that
    is too short gets a "user-" prefix ("ab" -> "user-ab", "!!" -> "user").
    """
    base = NOT_USERNAME_CHARACTERS.sub("", email.split("@", 1)[0].lower())
    base = base[:USERNAME_BASE_MAX_LENGTH]
    if len(base) < USERNAME_MIN_LENGTH:
        base = f"user-{base}" if base else "user"
    return base


def username_candidates(email: str) -> Iterator[str]:
    """Yield the usernames to try, in order: "aline", then "aline4821", ..."""
    base = username_base(email)
    yield base
    for _ in range(USERNAME_ATTEMPTS - 1):
        yield f"{base}{username_suffix()}"


class AuthService:
    """Register users, check logins and issue tokens. Owns its transactions."""

    def __init__(self, session: AsyncSession, users: UserRepository, settings: Settings) -> None:
        """Keep the session, the user repository and the settings (token lifetimes, key)."""
        self._session = session
        self._users = users
        self._settings = settings

    async def register(self, data: UserRegister) -> User:
        """Create a new account; only the password's hash is stored.

        The unique constraints in Postgres are the real guard: they also catch two
        registrations at the same moment, which a "SELECT first" check would miss.

        Raises:
            ConflictError: The email (or the username the client chose) is taken,
                or no free username could be made (409).
        """
        # Argon2 is slow on purpose: hash ONCE, in a thread, before any transaction
        # is opened, so no database connection waits for it
        hashed = await asyncio.to_thread(hash_password, data.password.get_secret_value())
        chosen = data.username
        candidates = [chosen] if chosen else username_candidates(data.email)

        for username in candidates:
            try:
                async with self._session.begin():
                    user = User(email=data.email, username=username, hashed_password=hashed)
                    return await self._users.add(user)
            except IntegrityError as exc:
                constraint = unique_violation_constraint(exc)
                if constraint == "uq_users_username" and chosen is None:
                    continue  # our own made-up name was taken: try the next one
                taken = TAKEN_MESSAGES.get(constraint or "")
                if taken is None:
                    raise  # another constraint: the global handler decides
                field, message = taken
                raise ConflictError(message, details={"field": field}) from exc

        raise ConflictError(
            "Could not find a free username. Please choose one and send it as 'username'.",
            details={"field": "username"},
        )

    async def login(self, email: str, password: str) -> TokenPair:
        """Check the email and password; return a new access + refresh token pair.

        Raises:
            InvalidCredentialsError: Unknown email, wrong password or switched-off
                account. The same error every time, so nobody can find out which
                emails are registered.
        """
        email = email.strip().lower()
        # A password longer than any allowed one can never be right: refuse it before
        # the slow hash, so huge inputs cannot be used to load the server
        if len(password) > PASSWORD_MAX_LENGTH or len(email) > EMAIL_MAX_LENGTH:
            raise InvalidCredentialsError()

        async with self._session.begin():  # a short read; closed before hashing
            user = await self._users.get_by_email(email)
            user_id = user.id if user else None
            is_active = user.is_active if user else False
            stored_hash = user.hashed_password if user else DUMMY_HASH

        # Always ONE slow verification, even for an unknown email (same response time)
        password_ok = await asyncio.to_thread(verify_password, password, stored_hash)
        if user_id is None or not password_ok or not is_active:
            raise InvalidCredentialsError()

        async with self._session.begin():
            await self._users.record_login(user_id)
        return self._token_pair(user_id)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Swap a valid refresh token for a new token pair.

        The user is read again: a deleted or switched-off user gets nothing, even
        with a refresh token that has not expired yet.

        Raises:
            InvalidTokenError: Bad, expired or access-type token, or the user is
                gone or switched off (401).
        """
        user_id = decode_token(refresh_token, TokenType.REFRESH, self._settings)
        async with self._session.begin():
            user = await self._users.get(user_id)
            allowed = user is not None and user.is_active
        if not allowed:
            raise InvalidTokenError()
        return self._token_pair(user_id)

    def _token_pair(self, user_id: int) -> TokenPair:
        """Create a fresh access + refresh token pair for this user."""
        return TokenPair(
            access_token=create_access_token(user_id, self._settings),
            refresh_token=create_refresh_token(user_id, self._settings),
        )
