"""The auth service: register a user, log in, and swap a refresh token for new tokens."""

import logging
import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import account_limit
from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    InvalidCredentialsError,
    InvalidTokenError,
    TooManyAttemptsError,
)
from app.core.logging import mask_email
from app.core.security import (
    DUMMY_HASH,
    TokenType,
    create_access_token,
    decode_token_claims,
    hash_password,
    is_revoked,
    issue_refresh_token,
    verify_password,
)
from app.db.errors import unique_violation_constraint
from app.models import User
from app.repositories import RefreshTokenRepository, UserRepository
from app.schemas.auth import EMAIL_MAX_LENGTH, PASSWORD_MAX_LENGTH, TokenPair, UserRegister
from app.schemas.user import USERNAME_MIN_LENGTH
from app.services.password_work import run_password_work

security_logger = logging.getLogger("app.security")

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

    def __init__(
        self,
        session: AsyncSession,
        users: UserRepository,
        settings: Settings,
        refresh_tokens: RefreshTokenRepository | None = None,
    ) -> None:
        """Keep the session, the repositories and the settings (token lifetimes, key)."""
        self._session = session
        self._users = users
        self._settings = settings
        self._refresh_tokens = refresh_tokens or RefreshTokenRepository(session)

    async def register(self, data: UserRegister) -> User:
        """Create a new account; only the password's hash is stored.

        The unique constraints in Postgres are the real guard: they also catch two
        registrations at the same moment, which a "SELECT first" check would miss.

        Raises:
            ConflictError: The email (or the username the client chose) is taken,
                or no free username could be made (409).
        """
        # A taken email can be refused cheaply: do it BEFORE the slow (64 MB) hash, so
        # a flood of repeated registrations cannot keep the server busy hashing
        async with self._session.begin():
            if await self._users.get_by_email(data.email) is not None:
                field, message = EMAIL_TAKEN
                raise ConflictError(message, details={"field": field})
        # Argon2 is slow on purpose: hash ONCE, in the small password pool, with no
        # transaction open, so no database connection waits for it
        hashed = await run_password_work(hash_password, data.password.get_secret_value())
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
            TooManyAttemptsError: This email already had LOGIN_ACCOUNT_RATE_LIMIT
                failed logins (429), whether the account exists or not.
        """
        email = email.strip().lower()
        # A password longer than any allowed one can never be right: refuse it before
        # the slow hash, so huge inputs cannot be used to load the server.
        # A NUL byte can be in no stored email (and Postgres refuses it in text).
        if len(password) > PASSWORD_MAX_LENGTH or len(email) > EMAIL_MAX_LENGTH or "\x00" in email:
            raise InvalidCredentialsError()
        # Per-ACCOUNT limit, checked before the slow hash. Unknown emails are counted
        # the same way, so a 429 says nothing about whether the account exists.
        if account_limit.is_blocked(email):
            security_logger.warning("Account login limit hit for %s", mask_email(email))
            raise TooManyAttemptsError(account_limit.retry_after_seconds())

        async with self._session.begin():  # a short read; closed before hashing
            user = await self._users.get_by_email(email)
            user_id = user.id if user else None
            is_active = user.is_active if user else False
            stored_hash = user.hashed_password if user else DUMMY_HASH

        # Always ONE slow verification, even for an unknown email (same response time)
        password_ok = await run_password_work(verify_password, password, stored_hash)
        if user_id is None or not password_ok or not is_active:
            account_limit.record_failure(email)
            raise InvalidCredentialsError()

        account_limit.clear_failures(email)  # the real user is in: forget the typos
        async with self._session.begin():
            await self._users.record_login(user_id)
            await self._refresh_tokens.delete_expired(user_id)  # housekeeping
        return await self.issue_tokens(user_id)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Swap a refresh token for a new pair. Each refresh token works ONCE.

        Rotation: the token is marked used, and the new pair has a new refresh token.
        Reuse detection: if a token that was ALREADY used comes back, someone kept a
        copy (stolen, or replayed). Thief and user cannot be told apart, so every
        refresh token of that user is revoked; both must log in again.
        The user is read again: a deleted or switched-off user gets nothing.

        Raises:
            InvalidTokenError: Bad, expired, unknown, already used or access-type
                token, or the user is gone or switched off (401).
        """
        claims = decode_token_claims(refresh_token, TokenType.REFRESH, self._settings)
        reused = False
        async with self._session.begin():
            first_use = await self._refresh_tokens.use(claims.jti, claims.user_id)
            if not first_use and await self._refresh_tokens.was_used(claims.jti, claims.user_id):
                reused = True
                await self._refresh_tokens.revoke_all(claims.user_id)
                # ...and the ACCESS tokens too, at once (not up to 15 minutes later)
                await self._users.revoke_tokens(claims.user_id, datetime.now(UTC))
            user = await self._users.get(claims.user_id)
            allowed = (
                first_use
                and user is not None
                and user.is_active
                and not is_revoked(claims.issued_at, user.tokens_valid_after)
            )
        # Raise only AFTER the block: raising inside it would roll the revocation back
        if reused:
            security_logger.warning(
                "Refresh token reuse for user_id=%s: all tokens revoked",
                claims.user_id,
            )
        if not allowed:
            raise InvalidTokenError()
        return await self.issue_tokens(claims.user_id)

    async def issue_tokens(self, user_id: int) -> TokenPair:
        """Create a fresh access + refresh token pair, and store the refresh token's id."""
        refresh = issue_refresh_token(user_id, self._settings)
        async with self._session.begin():
            await self._refresh_tokens.add(refresh.jti, user_id, refresh.expires_at)
        return TokenPair(
            access_token=create_access_token(user_id, self._settings),
            refresh_token=refresh.token,
        )
