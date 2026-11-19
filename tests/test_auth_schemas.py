"""The register, user-out, token and refresh schemas (no database needed)."""

import pytest
from pydantic import ValidationError

from app.models import User, UserTier
from app.schemas.auth import RefreshIn, TokenPair, UserOut, UserRegister

PASSWORD = "a-long-passphrase"


def error_fields(error: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return {str(item["loc"][0]) for item in error.value.errors()}


# ---------- UserRegister ----------


def test_register_needs_only_email_and_password() -> None:
    data = UserRegister(email="Aline@Example.COM", password=PASSWORD)

    assert data.email == "aline@example.com"  # lower-cased
    assert data.username is None  # the server makes one later
    assert data.password.get_secret_value() == PASSWORD


def test_register_accepts_a_username() -> None:
    assert UserRegister(email="a@example.com", password=PASSWORD, username="aline").username == (
        "aline"
    )


@pytest.mark.parametrize("length", [8, 128])
def test_register_password_length_limits_are_allowed(length: int) -> None:
    UserRegister(email="a@example.com", password="x" * length)


@pytest.mark.parametrize("length", [7, 129])
def test_register_password_outside_8_to_128_is_rejected(length: int) -> None:
    with pytest.raises(ValidationError) as error:
        UserRegister(email="a@example.com", password="x" * length)
    assert error_fields(error) == {"password"}


@pytest.mark.parametrize("field", ["tier", "is_staff", "is_superuser", "id", "hashed_password"])
def test_register_refuses_extra_fields(field: str) -> None:
    with pytest.raises(ValidationError) as error:
        UserRegister.model_validate({"email": "a@example.com", "password": PASSWORD, field: "x"})
    assert error_fields(error) == {field}


def test_register_rejects_bad_email_and_username() -> None:
    with pytest.raises(ValidationError) as error:
        UserRegister(email="not-an-email", password=PASSWORD, username="a!")
    assert error_fields(error) == {"email", "username"}


@pytest.mark.parametrize(
    "email",
    [
        "\N{CYRILLIC SMALL LETTER A}line@example.com",  # looks exactly like aline@example.com
        "jos\N{LATIN SMALL LETTER E WITH ACUTE}@example.com",
        "aline@xn--exmple-cua.com",  # an internationalised domain, decoded to "exämple"
    ],
)
def test_register_accepts_only_ascii_emails(email: str) -> None:
    with pytest.raises(ValidationError) as error:
        UserRegister(email=email, password=PASSWORD)
    assert error_fields(error) == {"email"}


def test_register_strips_spaces_around_the_email() -> None:
    assert UserRegister(email="  aline@example.com ", password=PASSWORD).email == (
        "aline@example.com"
    )


@pytest.mark.parametrize("password", [" " * 8, "\t" * 10, " \n " * 4])
def test_register_refuses_a_password_of_only_spaces(password: str) -> None:
    with pytest.raises(ValidationError) as error:
        UserRegister(email="a@example.com", password=password)
    assert error_fields(error) == {"password"}


def test_register_hides_the_password_when_printed() -> None:
    assert PASSWORD not in repr(UserRegister(email="a@example.com", password=PASSWORD))


# ---------- UserOut ----------


def test_user_out_shows_only_id_email_and_tier() -> None:
    user = User(id=5, email="a@example.com", username="a", hashed_password="h", tier=UserTier.FREE)

    assert UserOut.model_validate(user).model_dump(mode="json") == {
        "id": 5,
        "email": "a@example.com",
        "tier": "free",
    }


# ---------- TokenPair and RefreshIn ----------


def test_token_pair_type_is_always_bearer() -> None:
    pair = TokenPair(access_token="a", refresh_token="r")

    assert pair.model_dump() == {"access_token": "a", "refresh_token": "r", "token_type": "bearer"}
    with pytest.raises(ValidationError):
        TokenPair(access_token="a", refresh_token="r", token_type="basic")  # type: ignore[arg-type]


def test_refresh_in_hides_the_token_when_printed() -> None:
    data = RefreshIn(refresh_token="secret.jwt.value")

    assert "secret.jwt.value" not in repr(data)
    assert data.refresh_token.get_secret_value() == "secret.jwt.value"


@pytest.mark.parametrize(
    "body",
    [{}, {"refresh_token": ""}, {"refresh_token": "x" * 2049}, {"refresh_token": "t", "extra": 1}],
)
def test_refresh_in_rejects_bad_bodies(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RefreshIn.model_validate(body)
