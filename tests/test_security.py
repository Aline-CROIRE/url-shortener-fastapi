"""Unit tests for app/core/security.py (no database, no HTTP)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import InvalidTokenError
from app.core.security import (
    DUMMY_HASH,
    REQUIRED_CLAIMS,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

PASSWORD = "a-long-password"
USER_ID = 42


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


# ---------- JWT: creating tokens ----------


def read_claims(token: str) -> dict[str, Any]:
    """Read a token's payload WITHOUT checking it (anyone can do this: signed, not secret)."""
    return jwt.decode(token, options={"verify_signature": False})


def sign(claims: dict[str, Any], settings: Settings, algorithm: str = "HS256") -> str:
    """Sign hand-made claims with the test key, to build broken tokens on purpose."""
    return jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm=algorithm)


def valid_claims(**changes: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims = {
        "sub": str(USER_ID),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": "abc",
    }
    return claims | changes


def test_access_token_has_only_the_five_claims(test_settings: Settings) -> None:
    claims = read_claims(create_access_token(USER_ID, test_settings))

    assert sorted(claims) == sorted(REQUIRED_CLAIMS)
    assert claims["sub"] == "42"  # a string, as the JWT standard requires
    assert claims["type"] == "access"


def test_access_token_lives_15_minutes(test_settings: Settings) -> None:
    claims = read_claims(create_access_token(USER_ID, test_settings))

    assert claims["exp"] - claims["iat"] == 15 * 60


def test_refresh_token_lives_7_days(test_settings: Settings) -> None:
    claims = read_claims(create_refresh_token(USER_ID, test_settings))

    assert claims["type"] == "refresh"
    assert claims["exp"] - claims["iat"] == 7 * 24 * 60 * 60


def test_every_token_gets_its_own_jti(test_settings: Settings) -> None:
    first = read_claims(create_access_token(USER_ID, test_settings))
    second = read_claims(create_access_token(USER_ID, test_settings))

    assert first["jti"] != second["jti"]


def test_token_header_says_hs256(test_settings: Settings) -> None:
    assert jwt.get_unverified_header(create_access_token(USER_ID, test_settings))["alg"] == "HS256"


# ---------- JWT: decoding tokens ----------


def test_valid_access_token_returns_the_user_id(test_settings: Settings) -> None:
    token = create_access_token(USER_ID, test_settings)

    assert decode_token(token, TokenType.ACCESS, test_settings) == USER_ID


def test_valid_refresh_token_returns_the_user_id(test_settings: Settings) -> None:
    token = create_refresh_token(USER_ID, test_settings)

    assert decode_token(token, TokenType.REFRESH, test_settings) == USER_ID


def test_refresh_token_is_refused_as_an_access_token(test_settings: Settings) -> None:
    token = create_refresh_token(USER_ID, test_settings)

    with pytest.raises(InvalidTokenError):
        decode_token(token, TokenType.ACCESS, test_settings)


def test_access_token_is_refused_as_a_refresh_token(test_settings: Settings) -> None:
    token = create_access_token(USER_ID, test_settings)

    with pytest.raises(InvalidTokenError):
        decode_token(token, TokenType.REFRESH, test_settings)


def test_tampered_token_is_refused(test_settings: Settings) -> None:
    header, _payload, signature = create_access_token(USER_ID, test_settings).split(".")
    # Bob swaps in a payload that says he is user 1, but keeps the old signature
    forged_payload = jwt.utils.base64url_encode(b'{"sub":"1","type":"access"}').decode()

    with pytest.raises(InvalidTokenError):
        decode_token(f"{header}.{forged_payload}.{signature}", TokenType.ACCESS, test_settings)


def test_token_signed_with_another_key_is_refused(test_settings: Settings) -> None:
    token = jwt.encode(valid_claims(), "another-key-that-is-at-least-32-chars!!", "HS256")

    with pytest.raises(InvalidTokenError):
        decode_token(token, TokenType.ACCESS, test_settings)


def test_expired_token_is_refused(test_settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(minutes=20)
    token = sign(valid_claims(iat=past, exp=past + timedelta(minutes=15)), test_settings)

    with pytest.raises(InvalidTokenError):
        decode_token(token, TokenType.ACCESS, test_settings)


def test_alg_none_token_is_refused(test_settings: Settings) -> None:
    unsigned = jwt.encode(valid_claims(), None, algorithm="none")  # no signature at all

    with pytest.raises(InvalidTokenError):
        decode_token(unsigned, TokenType.ACCESS, test_settings)


@pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
def test_token_missing_a_claim_is_refused(test_settings: Settings, claim: str) -> None:
    claims = valid_claims()
    del claims[claim]

    with pytest.raises(InvalidTokenError):
        decode_token(sign(claims, test_settings), TokenType.ACCESS, test_settings)


@pytest.mark.parametrize("subject", ["abc", "", "-1", "4.2"])
def test_token_with_a_non_numeric_user_id_is_refused(test_settings: Settings, subject: str) -> None:
    token = sign(valid_claims(sub=subject), test_settings)

    with pytest.raises(InvalidTokenError):
        decode_token(token, TokenType.ACCESS, test_settings)


@pytest.mark.parametrize("garbage", ["", "not-a-token", "a.b.c"])
def test_garbage_is_refused(test_settings: Settings, garbage: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(garbage, TokenType.ACCESS, test_settings)
