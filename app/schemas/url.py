from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.models.url import SHORT_CODE_MAX_LENGTH
from app.schemas.base import ORMModel
from app.schemas.tag import TagName, TagRead
from app.schemas.user import OwnerRead

ALIAS_PATTERN = r"^[A-Za-z0-9_-]+$"
# Aliases that would clash with the app's own paths (/docs, /health, /api/...)
RESERVED_ALIASES = frozenset({"api", "docs", "redoc", "health", "openapi", "static", "admin"})
MAX_TAGS_PER_URL = 10


# ---------- reusable field types ----------


def check_in_future(expires_at: datetime) -> datetime:
    if expires_at <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return expires_at


def remove_duplicate_tags(tags: list[str]) -> list[str]:
    """Keep the first spelling of each tag, ignoring case: ["Ads", "ads"] -> ["Ads"]."""
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            unique.append(tag)
    return unique


# A timezone-aware datetime that must be in the future
FutureDatetime = Annotated[AwareDatetime, AfterValidator(check_in_future)]
# Up to 10 clean tag names, duplicates (ignoring case) removed
TagList = Annotated[
    list[TagName], Field(max_length=MAX_TAGS_PER_URL), AfterValidator(remove_duplicate_tags)
]


# ---------- input schemas ----------


class URLCreate(BaseModel):
    """What a client sends to create a short URL."""

    model_config = ConfigDict(extra="forbid")  # unknown fields -> 422, not silently ignored

    original_url: HttpUrl = Field(examples=["https://www.python.org/about/"])
    custom_alias: str | None = Field(
        default=None,
        min_length=3,
        max_length=SHORT_CODE_MAX_LENGTH,
        pattern=ALIAS_PATTERN,
        description="Your own short code: 3-10 letters, digits, '-' or '_'",
        examples=["py-docs"],
    )
    expires_at: FutureDatetime | None = Field(
        default=None,
        description="When the link stops working (must include a timezone)",
        examples=["2027-01-31T23:59:59Z"],
    )
    tags: TagList = Field(default_factory=list, examples=[["Marketing", "Social"]])

    @field_validator("custom_alias")
    @classmethod
    def alias_is_not_reserved(cls, alias: str | None) -> str | None:
        if alias is not None and alias.lower() in RESERVED_ALIASES:
            raise ValueError(f"'{alias}' is reserved, please choose another alias")
        return alias


class URLUpdate(BaseModel):
    """Fields a client may change. Every field is optional: only the fields that
    are SENT are changed (read them with model_dump(exclude_unset=True)).

    short_code and custom_alias are not here on purpose: changing them would
    break links that people already shared.
    """

    model_config = ConfigDict(extra="forbid")

    original_url: HttpUrl | None = None
    is_active: bool | None = None
    expires_at: FutureDatetime | None = Field(
        default=None, description="Send null to remove the expiry date"
    )
    title: str | None = Field(default=None, max_length=255)
    description: str | None = None
    tags: TagList | None = Field(default=None, description="Replaces all tags")

    @field_validator("original_url", "is_active", "tags")
    @classmethod
    def cannot_be_null(cls, value: object) -> object:
        # Runs only for values the client SENT. "Not sent" is fine; an explicit
        # null is not, because these columns can never be empty.
        if value is None:
            raise ValueError("cannot be null")
        return value


# ---------- output schema ----------


class URLRead(ORMModel):
    """A short URL as the API shows it, with its owner and tags.

    Build it with: URLRead.model_validate(url, context={"base_url": ...})
    """

    id: int
    short_code: str
    short_url: str = ""  # filled from the validation context below
    original_url: str
    custom_alias: str | None
    is_active: bool
    expires_at: datetime | None
    title: str | None
    description: str | None
    favicon: str | None
    click_count: int
    created_at: datetime
    updated_at: datetime
    owner: OwnerRead
    tags: list[TagRead]

    @model_validator(mode="after")
    def build_short_url(self, info: ValidationInfo) -> Self:
        if self.short_url:
            # Already built: FastAPI validates the returned URLRead a second time
            # (without our context) before sending it; keep the value we made.
            return self
        # base_url is a setting, not a column, so the caller passes it in
        base_url = (info.context or {}).get("base_url")
        if not base_url:
            raise ValueError("URLRead needs context={'base_url': ...} to build short_url")
        self.short_url = f"{base_url.rstrip('/')}/{self.short_code}"
        return self
