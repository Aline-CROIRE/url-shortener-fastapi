"""Request shapes for logging in (registering uses UserCreate)."""

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator


class LoginRequest(BaseModel):
    """The email and password of an existing account."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(examples=["aline@example.com"])
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, email: str) -> str:
        """Lower-case the email, as it was stored at registration."""
        return email.lower()
