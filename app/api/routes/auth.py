"""Endpoints to register, log in, refresh tokens and see who is logged in."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.auth import CurrentUser
from app.api.dependencies import AuthServiceDep
from app.api.responses import CONFLICT, INTERNAL_ERROR, UNAUTHORIZED, VALIDATION_ERROR
from app.schemas.auth import RefreshIn, TokenPair, UserOut, UserRegister
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["Auth"])

REGISTER_EXAMPLE = {"email": "aline@example.com", "password": "a-long-passphrase"}


def no_store(response: Response) -> None:
    """Forbid every cache (browser, proxy) to keep a token response (RFC 6749, 5.1)."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/register", status_code=status.HTTP_201_CREATED, include_in_schema=False)
@router.post(
    "/register/",
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    description=(
        "Only `email` and `password` (8-128 characters) are needed; `username` is "
        "optional and made from the email when missing. Every account starts on the "
        "free tier. Only a hash of the password is stored."
    ),
    responses={**CONFLICT, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def register(
    payload: Annotated[UserRegister, Body(examples=[REGISTER_EXAMPLE])], service: AuthServiceDep
) -> UserOut:
    """Create an account."""
    user = await service.register(payload)
    return UserOut.model_validate(user)


@router.post("/login", include_in_schema=False)
@router.post(
    "/login/",
    summary="Log in",
    description=(
        "OAuth2 password flow: send a FORM with your **email in `username`** and your "
        "`password`. Returns a short-lived access token (send it as "
        "`Authorization: Bearer <token>`) and a long-lived refresh token."
    ),
    responses={**UNAUTHORIZED, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    service: AuthServiceDep,
    response: Response,
) -> TokenPair:
    """Log in and get a token pair."""
    tokens = await service.login(form.username, form.password)
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
