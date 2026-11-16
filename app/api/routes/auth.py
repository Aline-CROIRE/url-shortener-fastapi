"""Endpoints to register, log in, log out and see who is logged in."""

from typing import Annotated

from fastapi import APIRouter, Body, Request, Response, status

from app.api.auth import CurrentUser
from app.api.dependencies import AuthServiceDep
from app.api.responses import CONFLICT, INTERNAL_ERROR, UNAUTHORIZED, VALIDATION_ERROR
from app.core.security import SESSION_USER_KEY
from app.schemas.auth import LoginRequest
from app.schemas.user import UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["Auth"])

REGISTER_EXAMPLE = {
    "email": "aline@example.com",
    "username": "aline",
    "password": "a-long-password",
}
LOGIN_EXAMPLE = {"email": "aline@example.com", "password": "a-long-password"}


@router.post("/register", status_code=status.HTTP_201_CREATED, include_in_schema=False)
@router.post(
    "/register/",
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    description="Passwords need at least 8 characters; only a hash of the password is stored.",
    responses={**CONFLICT, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def register(
    payload: Annotated[UserCreate, Body(examples=[REGISTER_EXAMPLE])], service: AuthServiceDep
) -> UserRead:
    """Create an account."""
    user = await service.register(payload)
    return UserRead.model_validate(user)


@router.post("/login", include_in_schema=False)
@router.post(
    "/login/",
    summary="Log in",
    description=(
        "Checks the email and password, then sets a signed `session` cookie. "
        "The browser (and Swagger) sends it back automatically, so every later "
        "request knows who you are."
    ),
    responses={**UNAUTHORIZED, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def login(
    request: Request,
    payload: Annotated[LoginRequest, Body(examples=[LOGIN_EXAMPLE])],
    service: AuthServiceDep,
) -> UserRead:
    """Log in and start a session."""
    user = await service.login(payload)
    request.session.clear()  # start a fresh session, never reuse an old one
    request.session[SESSION_USER_KEY] = user.id
    return UserRead.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
@router.post(
    "/logout/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out",
    description="Clears the session cookie. Works even if you are not logged in.",
)
async def logout(request: Request) -> Response:
    """Log out by clearing the session."""
    request.session.clear()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", include_in_schema=False)
@router.get(
    "/me/",
    summary="Who am I?",
    description="Returns the logged-in user.",
    responses={**UNAUTHORIZED, **INTERNAL_ERROR},
)
async def me(user: CurrentUser) -> UserRead:
    """Return the logged-in user."""
    return UserRead.model_validate(user)
