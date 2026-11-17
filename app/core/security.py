"""Password hashing (Argon2id via pwdlib) and the name of the login session cookie."""

import secrets

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

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
