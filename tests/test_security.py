"""Unit tests for app/core/security.py (no database, no HTTP)."""

from app.core.security import DUMMY_HASH, hash_password, verify_password

PASSWORD = "a-long-password"


# ---------- password hashing ----------


def test_hash_is_argon2id() -> None:
    assert hash_password(PASSWORD).startswith("$argon2id$")


def test_hash_never_contains_the_password() -> None:
    assert PASSWORD not in hash_password(PASSWORD)


def test_same_password_gives_different_hashes_because_of_the_salt() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_right_password_is_verified() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_wrong_password_is_refused() -> None:
    assert verify_password("wrong-password", hash_password(PASSWORD)) is False


def test_a_value_that_is_not_a_hash_is_refused_without_crashing() -> None:
    # e.g. "!" marks an account that can never log in with a password
    assert verify_password(PASSWORD, "!") is False


def test_dummy_hash_is_a_real_argon2id_hash_that_matches_nothing_guessable() -> None:
    assert DUMMY_HASH.startswith("$argon2id$")
    assert verify_password("", DUMMY_HASH) is False
    assert verify_password(PASSWORD, DUMMY_HASH) is False
