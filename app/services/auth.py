"""The auth service: register a user and check a login."""

import asyncio

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, InvalidCredentialsError
from app.core.security import DUMMY_PASSWORD_HASH, hash_password, verify_password
from app.db.errors import unique_violation_constraint
from app.models import User
from app.repositories import UserRepository
from app.schemas.auth import LoginRequest
from app.schemas.user import UserCreate

TAKEN_MESSAGES = {
    "uq_users_email": ("email", "This email is already registered."),
    "uq_users_username": ("username", "This username is already taken."),
}


class AuthService:
    """Register users and check logins. Owns its transactions (session.begin())."""

    def __init__(self, session: AsyncSession, users: UserRepository) -> None:
        """Keep the session and the user repository."""
        self._session = session
        self._users = users

    async def register(self, data: UserCreate) -> User:
        """Create a new account; only the password's hash is stored.

        Raises:
            ConflictError: The email or username is already used (409).
        """
        # Argon2 is slow on purpose: run it in a thread so other requests are not blocked
        hashed = await asyncio.to_thread(hash_password, data.password.get_secret_value())
        user = User(
            email=data.email,
            username=data.username,
            first_name=data.first_name,
            last_name=data.last_name,
            hashed_password=hashed,
        )
        try:
            async with self._session.begin():
                await self._users.add(user)
        except IntegrityError as exc:  # the unique constraints are the real guard
            taken = TAKEN_MESSAGES.get(unique_violation_constraint(exc) or "")
            if taken is None:
                raise
            field, message = taken
            raise ConflictError(message, details={"field": field}) from exc
        return user

    async def login(self, data: LoginRequest) -> User:
        """Return the user if the email and password are right, and record the login time.

        Raises:
            InvalidCredentialsError: Unknown email, wrong password or inactive account.
                The same error every time, so nobody can find out which emails exist.
        """
        password = data.password.get_secret_value()
        async with self._session.begin():
            user = await self._users.get_by_email(data.email)
            # Always run one (slow) verification, even for an unknown email
            stored_hash = user.hashed_password if user else DUMMY_PASSWORD_HASH
            password_ok = await asyncio.to_thread(verify_password, password, stored_hash)
            if user is None or not password_ok or not user.is_active:
                raise InvalidCredentialsError()
            user.last_login = func.now()
        return user
