"""Password hashing (Argon2id) and the name of the login session cookie."""

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Argon2id with the library's recommended settings: slow on purpose, so guessing
# passwords from a stolen database takes far too long
_hasher = PasswordHasher()

SESSION_COOKIE = "session"  # the cookie the browser sends back after login
SESSION_USER_KEY = "user_id"  # what the signed session cookie stores

# A real hash of a random password: when the email is unknown, login still runs
# one verification, so the response time does not reveal which emails exist
DUMMY_PASSWORD_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    """Return the Argon2id hash of a password (the only form that is stored)."""
    return _hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Return True if the password matches the stored hash.

    A wrong password, or a value that is not a real hash (like "!" for accounts
    that cannot log in), returns False instead of raising.
    """
    try:
        return _hasher.verify(hashed_password, password)
    except (VerificationError, InvalidHashError):
        return False
