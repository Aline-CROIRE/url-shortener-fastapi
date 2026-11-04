from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class URLCreate(BaseModel):
    original_url: HttpUrl = Field(
        description="The long URL to shorten. Must start with http:// or https://",
        examples=["https://www.python.org/about/"],
    )


class URLRead(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "short_code": "aB3dE9x",
                    "original_url": "https://www.python.org/about/",
                    "short_url": "http://localhost:8000/aB3dE9x",
                    "created_at": "2026-11-04T10:15:30.123456Z",
                }
            ]
        }
    )

    short_code: str = Field(description="7-character code made of letters and digits")
    original_url: str = Field(description="The long URL the short code points to")
    short_url: str = Field(description="Full short link to share")
    created_at: datetime = Field(description="When the short URL was created (UTC)")
