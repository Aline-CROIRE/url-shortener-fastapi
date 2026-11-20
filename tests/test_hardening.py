"""Worst cases found by the adversarial review, each one pinned by a test.

Every case here used to give a 500, waste the server's memory or CPU, leak a hint,
or weaken a protection. Now each one fails CLOSED with a clean 4xx.
"""

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.core.security as security_module
import app.services.auth as auth_service_module
from app.core.config import Settings
from app.core.error_handlers import unhandled_exception_handler
from app.core.exceptions import InvalidTokenError
from app.core.middleware import BodySizeLimitMiddleware
from app.core.rate_limit import limiter, rate_limit_key
from app.core.security import DUMMY_HASH, TokenType, decode_token, verify_password
from app.models import RefreshToken
from app.services.password_work import MAX_PARALLEL_PASSWORD_WORK, run_password_work
from tests.conftest import AuthedUser

AUTH = "/api/v1/auth"
URLS = "/api/v1/urls"
TARGET = "https://www.python.org/"
PASSWORD = "a-long-passphrase"

MakeUser = Callable[..., AuthedUser]


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


# ---------- 1. a NUL byte (\x00) never reaches Postgres ----------


def test_a_nul_byte_in_the_login_email_is_a_normal_401(
    db_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    response = db_client.post(
        f"{AUTH}/login/", data={"username": "a\x00@x.com", "password": "whatever-1"}
    )

    assert response.status_code == 401  # was 500: Postgres refuses \x00 in text
    assert error_code(response) == "invalid_credentials"
    assert any("Failed login" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", URLS + "/", {"original_url": TARGET, "tags": ["a\x00b"]}),
        ("get", URLS + "/?search=a%00b", None),
        ("get", URLS + "/?tag=a%00b", None),
    ],
    ids=["tag_on_create", "search", "tag_filter"],
)
def test_a_nul_byte_in_url_input_is_422(
    db_client: TestClient, make_user: MakeUser, method: str, path: str, body: Any
) -> None:
    user = make_user()
    kwargs: dict[str, Any] = {"headers": user.headers}
    if body is not None:
        kwargs["json"] = body

    response = getattr(db_client, method)(path, **kwargs)

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["title", "description"])
def test_a_nul_byte_in_an_update_is_422(
    db_client: TestClient, make_user: MakeUser, field: str
) -> None:
    user = make_user()
    code = db_client.post(f"{URLS}/", json={"original_url": TARGET}, headers=user.headers).json()[
        "short_code"
    ]

    response = db_client.put(f"{URLS}/{code}/", json={field: "a\x00"}, headers=user.headers)

    assert response.status_code == 422


# ---------- 5. tags whose lower case differs in Python and Postgres ----------


@pytest.mark.parametrize(
    "tag",
    ["\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}stanbul", "Stra\N{LATIN SMALL LETTER SHARP S}e"],
)
def test_unusual_capital_letters_in_a_tag_work(
    db_client: TestClient, make_user: MakeUser, tag: str
) -> None:
    user = make_user()

    first = db_client.post(
        f"{URLS}/", json={"original_url": TARGET, "tags": [tag]}, headers=user.headers
    )
    second = db_client.post(
        f"{URLS}/", json={"original_url": TARGET, "tags": [tag]}, headers=user.headers
    )

    assert first.status_code == second.status_code == 201  # was 500 (KeyError)
    assert first.json()["tags"][0]["id"] == second.json()["tags"][0]["id"]  # one tag, reused


# ---------- 6. forged pagination cursors ----------


def forged_cursor(created_at: str, row_id: Any) -> str:
    raw = json.dumps({"sort": "newest", "values": [created_at, row_id]}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.parametrize(
    ("created_at", "row_id"),
    [
        ("2026-01-01T00:00:00+00:00", 2**70),  # bigger than any database integer
        ("2026-01-01T00:00:00+00:00", 2**31),  # just past the INTEGER column
        ("2026-01-01T00:00:00+00:00", 0),
        ("2026-01-01T00:00:00+00:00", -5),
        ("9999-12-31T23:59:59-23:59", 1),  # a date Postgres cannot store
    ],
    ids=["huge_id", "int32_overflow", "zero_id", "negative_id", "date_out_of_range"],
)
def test_a_forged_cursor_is_400_not_500(
    db_client: TestClient, owner_id: int, created_at: str, row_id: Any
) -> None:
    response = db_client.get(f"{URLS}/", params={"cursor": forged_cursor(created_at, row_id)})

    assert response.status_code == 400
    assert error_code(response) == "invalid_cursor"


# ---------- 9. size limits ----------


def test_a_description_is_at_most_2000_characters(db_client: TestClient, owner_id: int) -> None:
    code = db_client.post(f"{URLS}/", json={"original_url": TARGET}).json()["short_code"]

    assert db_client.put(f"{URLS}/{code}/", json={"description": "d" * 2000}).status_code == 200
    assert db_client.put(f"{URLS}/{code}/", json={"description": "d" * 2001}).status_code == 422


def test_a_huge_body_is_refused_with_413(db_client: TestClient, owner_id: int) -> None:
    body = json.dumps({"original_url": TARGET, "description": "x" * 200_000})

    response = db_client.post(
        f"{URLS}/", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert error_code(response) == "payload_too_large"


def test_a_huge_body_without_a_length_header_is_refused_too(db_client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:  # "Transfer-Encoding: chunked": no Content-Length
        for _ in range(100):
            yield b"x" * 10_000

    response = db_client.post(
        f"{AUTH}/register/", content=chunks(), headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413


# ---------- 3. register: limited per IP, and no slow hash for a taken email ----------


def test_register_is_limited_per_ip(db_client: TestClient) -> None:
    codes = [
        db_client.post(
            f"{AUTH}/register/", json={"email": f"user{n}@example.com", "password": PASSWORD}
        ).status_code
        for n in range(11)
    ]

    assert codes == [201] * 10 + [429]
    # The login counter is separate: logging in still works
    login = db_client.post(
        f"{AUTH}/login/", data={"username": "user0@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200


def test_a_taken_email_is_refused_before_the_slow_hash(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_client.post(f"{AUTH}/register/", json={"email": "taken@example.com", "password": PASSWORD})
    hashed: list[str] = []
    monkeypatch.setattr(auth_service_module, "hash_password", lambda p: hashed.append(p) or "h")

    response = db_client.post(
        f"{AUTH}/register/", json={"email": "TAKEN@example.com", "password": PASSWORD}
    )

    assert response.status_code == 409
    assert hashed == []  # no 64 MB Argon2 run for a request that must fail anyway


@pytest.mark.anyio
async def test_only_a_few_password_hashes_run_at_the_same_time() -> None:
    running = 0
    most = 0

    def slow_hash() -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        time.sleep(0.05)
        running -= 1

    await asyncio.gather(*(run_password_work(slow_hash) for _ in range(12)))

    assert most <= MAX_PARALLEL_PASSWORD_WORK  # each Argon2 run takes 64 MB of memory


# ---------- 4. the rate-limit key ----------


def request_from(host: str) -> Request:
    return Request({"type": "http", "headers": [], "client": (host, 1234)})


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("1.2.3.4", "::ffff:1.2.3.4"),  # the same IPv4 address, written as IPv6
        ("2001:db8::1", "2001:db8::ffff:abcd"),  # one home network (/64), many addresses
    ],
)
def test_one_client_cannot_get_several_rate_limit_buckets(first: str, second: str) -> None:
    assert rate_limit_key(request_from(first)) == rate_limit_key(request_from(second))


def test_different_clients_get_different_buckets() -> None:
    assert rate_limit_key(request_from("1.2.3.4")) != rate_limit_key(request_from("1.2.3.5"))
    assert rate_limit_key(request_from("2001:db8:0:1::1")) != rate_limit_key(
        request_from("2001:db8:0:2::1")
    )
    assert rate_limit_key(request_from("testclient")) == "testclient"  # not an IP: kept


def test_the_limit_is_a_sliding_window() -> None:
    # A fixed window allows 5 tries at 0:59 and 5 more at 1:00 (10 in one second)
    assert type(limiter.limiter).__name__ == "MovingWindowRateLimiter"


# ---------- 8. logs and request ids ----------


def test_control_characters_in_the_path_never_reach_the_request_log(
    db_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    db_client.get("/ab%1b[2Kc%c2%85X%e2%80%a8Y")

    request_lines = [r.getMessage() for r in caplog.records if r.name == "app.requests"]
    assert request_lines
    for line in request_lines:
        assert "\x1b" not in line and "\x85" not in line and "\N{LINE SEPARATOR}" not in line


@pytest.mark.parametrize(
    "bad_id",
    [b"abc\x85def", b"abc\x1b[2Kdef", b"has space", b"x" * 129, b"a;b"],
    ids=["next_line_byte", "escape_code", "space", "too_long", "semicolon"],
)
def test_a_strange_request_id_is_replaced(db_client: TestClient, bad_id: bytes) -> None:
    # Sent as raw bytes, the way an attacker's own client can send any byte
    response = db_client.get("/health", headers={"X-Request-ID": bad_id})

    assert response.headers["x-request-id"].encode("latin-1") != bad_id
    assert len(response.headers["x-request-id"]) == 32  # a fresh uuid4 hex


def test_a_normal_request_id_is_kept(db_client: TestClient) -> None:
    response = db_client.get("/health", headers={"X-Request-ID": "trace-1234_ab.CD"})

    assert response.headers["x-request-id"] == "trace-1234_ab.CD"


def test_a_500_has_the_security_headers_too(app: FastAPI, db_client: TestClient) -> None:
    @app.get("/_test/crash-headers")
    async def crash() -> None:
        raise RuntimeError("boom")

    response = db_client.get("/_test/crash-headers")

    assert response.status_code == 500
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


# ---------- 11 + 12. clock skew and strange user ids in tokens ----------


def signed(settings: Settings, **claims: Any) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": "1",
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": "j",
    }
    payload.update(claims)
    return jwt.encode(payload, settings.jwt_secret_key.get_secret_value(), "HS256")


def test_a_few_seconds_of_clock_difference_are_tolerated(test_settings: Settings) -> None:
    # Another server's clock is 10 s ahead: its fresh token must not be refused
    token = signed(test_settings, iat=datetime.now(UTC) + timedelta(seconds=10))

    assert decode_token(token, TokenType.ACCESS, test_settings) == 1


def test_a_token_from_well_in_the_future_is_refused(test_settings: Settings) -> None:
    future = datetime.now(UTC) + timedelta(minutes=5)
    token = signed(test_settings, iat=future, exp=future + timedelta(minutes=5))

    with pytest.raises(InvalidTokenError):
        decode_token(token, TokenType.ACCESS, test_settings)


@pytest.mark.parametrize("sub", ["007", "0", "2147483648", "9" * 20, "9" * 5000])
def test_strange_user_ids_in_a_signed_token_are_refused(test_settings: Settings, sub: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(signed(test_settings, sub=sub), TokenType.ACCESS, test_settings)


# ---------- 13. accounts without a password take as long as real ones ----------


def test_an_account_without_a_usable_password_still_runs_one_slow_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    real_verify = security_module._password_hash.verify

    def spy(password: str, hashed: str) -> bool:
        seen.append(hashed)
        return real_verify(password, hashed)

    monkeypatch.setattr(security_module._password_hash, "verify", spy)

    assert verify_password("any-password", "!") is False  # "!" = can never log in

    assert DUMMY_HASH in seen  # same Argon2 work as a real account: no timing hint


# ---------- small paths, tested directly ----------


@pytest.mark.anyio
async def test_a_client_that_leaves_mid_body_is_passed_on_cleanly() -> None:
    seen: list[dict[str, Any]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(await receive())  # the disconnect, handed on unchanged
        seen.append(await receive())  # later messages come from the client again

    messages = iter([{"type": "http.disconnect"}, {"type": "http.disconnect"}])

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        raise AssertionError("nothing may be sent for a client that left")

    await BodySizeLimitMiddleware(app)({"type": "http", "headers": []}, receive, send)

    assert seen == [{"type": "http.disconnect"}, {"type": "http.disconnect"}]


@pytest.mark.anyio
async def test_a_500_before_any_request_id_still_has_the_security_headers() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/x", "headers": []})

    response = await unhandled_exception_handler(request, RuntimeError("early"))

    assert response.status_code == 500
    assert "x-request-id" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"


def test_a_refresh_token_row_never_shows_its_id() -> None:
    row = RefreshToken(jti="secret-token-id", user_id=7, expires_at=datetime.now(UTC))

    assert "secret-token-id" not in repr(row)
    assert repr(row) == "<RefreshToken user_id=7 used=False>"
