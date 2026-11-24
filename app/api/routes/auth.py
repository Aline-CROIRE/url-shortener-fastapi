"""Endpoints to register, log in, refresh tokens and see who is logged in."""

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.auth import CurrentUser
from app.api.dependencies import AuthServiceDep
from app.api.responses import (
    CONFLICT,
    INTERNAL_ERROR,
    TOO_MANY_REQUESTS,
    UNAUTHORIZED,
    VALIDATION_ERROR,
)
from app.core.client import client_ip
from app.core.exceptions import InvalidCredentialsError
from app.core.logging import mask_email
from app.core.rate_limit import limiter, login_rate_limit, register_rate_limit
from app.schemas.auth import RefreshIn, RegisterAccepted, TokenPair, UserRegister
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["Auth"])
security_logger = logging.getLogger("app.security")

REGISTER_EXAMPLE = {"email": "aline@example.com", "password": "a-long-passphrase"}


def no_store(response: Response) -> None:
    """Forbid every cache (browser, proxy) to keep a token response (RFC 6749, 5.1)."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/register", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
@router.post(
    "/register/",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create an account",
    description=(
        "Only `email` and `password` (8-128 characters) are needed; `username` is "
        "optional and made from the email when missing. Every account starts on the "
        "free tier. Only a hash of the password is stored. **The answer is the same "
        "(202) whether the email is new or already registered**, so nobody can find "
        "out which emails exist: log in to use the account, and GET /auth/me/ shows "
        "it. A chosen `username` that is taken gives 409 (usernames are public). At "
        "most 10 registrations per hour per client (429 after that)."
    ),
    responses={**CONFLICT, **TOO_MANY_REQUESTS, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
# Each registration costs a slow (64 MB) password hash, and every account is a new
# Free quota: without a limit, one client could load the server or create endless
# accounts. Its own counter, separate from login.
@limiter.shared_limit(register_rate_limit, scope="register")
async def register(
    request: Request,  # slowapi reads the client from it
    payload: Annotated[UserRegister, Body(examples=[REGISTER_EXAMPLE])],
    service: AuthServiceDep,
) -> RegisterAccepted:
    """Create an account (one answer for every email)."""
    await service.register(payload)
    return RegisterAccepted()


@router.post("/login", include_in_schema=False)
@router.post(
    "/login/",
    summary="Log in",
    description=(
        "OAuth2 password flow: send a FORM with your **email in `username`** and your "
        "`password`. Returns a short-lived access token (send it as "
        "`Authorization: Bearer <token>`) and a long-lived refresh token. "
        "At most 5 attempts per minute per IP address (429 after that)."
    ),
    responses={**UNAUTHORIZED, **TOO_MANY_REQUESTS, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
# Counts EVERY attempt from one IP (right or wrong password, any email). shared_limit
# with one scope: "/login/" and "/login" use ONE counter (a plain limit() counts each
# path apart, which would give 5 + 5 tries).
@limiter.shared_limit(login_rate_limit, scope="login")
async def login(
    request: Request,  # slowapi reads the client IP from it
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    service: AuthServiceDep,
    response: Response,
) -> TokenPair:
    """Log in and get a token pair."""
    try:
        tokens = await service.login(form.username, form.password)
    except InvalidCredentialsError:
        # Who tried and from where; NEVER the password, and only a masked email
        security_logger.warning(
            "Failed login for %s from ip=%s", mask_email(form.username), client_ip(request)
        )
        raise
    no_store(response)
    return tokens


@router.post("/refresh", include_in_schema=False)
@router.post(
    "/refresh/",
    summary="Get new tokens",
    description=(
        "Send a refresh token, get a new access + refresh token pair. An access "
        "token is refused here, and a refresh token is refused everywhere else."
    ),
    responses={**UNAUTHORIZED, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def refresh(payload: RefreshIn, service: AuthServiceDep, response: Response) -> TokenPair:
    """Swap a refresh token for a new token pair."""
    tokens = await service.refresh(payload.refresh_token.get_secret_value())
    no_store(response)
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
@router.post(
    "/logout/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out (this device)",
    description=(
        "Send the refresh token of this device: its session ends, and it can no "
        "longer get new tokens. No access token is needed, so logging out also "
        "works after the access token has expired. The access token is not stored "
        "on the server: forget it on the client (it runs out within "
        "ACCESS_TOKEN_MINUTES). Calling it again is fine (204)."
    ),
    responses={**UNAUTHORIZED, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def logout(payload: RefreshIn, service: AuthServiceDep, response: Response) -> None:
    """End the session of one refresh token."""
    await service.logout(payload.refresh_token.get_secret_value())
    no_store(response)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
@router.post(
    "/logout-all/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out everywhere",
    description=(
        "Ends EVERY session of your account at once: all refresh tokens are revoked "
        "and every access token (this one too) stops working. Use it after a lost "
        "phone or a leaked password. Log in again to get new tokens."
    ),
    responses={**UNAUTHORIZED, **INTERNAL_ERROR},
)
async def logout_all(user: CurrentUser, service: AuthServiceDep, response: Response) -> None:
    """End every session of the current user."""
    await service.logout_everywhere(user.id)
    no_store(response)


@router.get("/me", include_in_schema=False)
@router.get(
    "/me/",
    summary="Who am I?",
    description="Returns the user of the access token, read fresh from the database.",
    responses={**UNAUTHORIZED, **INTERNAL_ERROR},
)
async def me(user: CurrentUser) -> UserRead:
    """Return the current user."""
    return UserRead.model_validate(user)
