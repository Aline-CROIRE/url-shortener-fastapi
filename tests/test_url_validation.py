"""What a short link may point to, and which aliases may be chosen (brief test #12).

original_url: http or https only, at most 2048 characters, no user name or password
inside it, and never this shortener itself (BASE_URL's host plus OWN_HOSTS).
custom_alias: 3-10 of A-Z a-z 0-9 _ -, never one of the app's own path names.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, normalize_host

URLS = "/api/v1/urls"
BASE = "https://example.com/"


def create(client: TestClient, **body: Any) -> Any:
    return client.post(f"{URLS}/", json=body)


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


# ---------- only real web links ----------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",  # brief test #12: would run code in the visitor's browser
        "JaVaScRiPt:alert(document.cookie)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "mailto:someone@example.com",
        "//example.com/no-scheme",
        "http://",
        "not a url",
        "",
    ],
)
def test_only_http_and_https_links_are_accepted(
    db_client: TestClient, owner_id: int, url: str
) -> None:
    response = create(db_client, original_url=url)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"


def test_the_same_rules_apply_when_updating(db_client: TestClient, owner_id: int) -> None:
    code = create(db_client, original_url=BASE).json()["short_code"]

    response = db_client.put(f"{URLS}/{code}/", json={"original_url": "javascript:alert(1)"})

    assert response.status_code == 422
    assert db_client.get(f"{URLS}/{code}/").json()["original_url"] == BASE


def test_a_url_of_2048_characters_is_accepted(db_client: TestClient, owner_id: int) -> None:
    url = BASE + "a" * (2048 - len(BASE))

    assert create(db_client, original_url=url).status_code == 201


def test_a_url_of_2049_characters_is_refused(db_client: TestClient, owner_id: int) -> None:
    url = BASE + "a" * (2049 - len(BASE))

    assert create(db_client, original_url=url).status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/",  # the password would be shown to every visitor
        "https://admin@example.com/",
        # A classic phishing trick: looks like google.com, really goes to evil.example
        "https://www.google.com@evil.example/login",
    ],
)
def test_a_url_with_a_user_name_or_password_is_refused(
    db_client: TestClient, owner_id: int, url: str
) -> None:
    response = create(db_client, original_url=url)

    assert response.status_code == 422
    assert "password" in response.text.lower()


def test_line_breaks_cannot_inject_a_header_into_the_redirect(
    db_client: TestClient, owner_id: int
) -> None:
    sneaky = "https://www.python.org/a\r\nSet-Cookie: hacked=1"
    code = create(db_client, original_url=sneaky).json()["short_code"]

    response = db_client.get(f"/{code}/")

    assert response.status_code == 302
    assert "\r" not in response.headers["location"]
    assert "\n" not in response.headers["location"]
    assert "set-cookie" not in response.headers  # no header was smuggled in


# ---------- never a link to ourselves (BASE_URL host + OWN_HOSTS) ----------


@pytest.mark.parametrize(
    "own_link",
    [
        "http://short.test./abc1234",  # a trailing dot is the same host
        "HTTPS://SHORT.TEST/abc1234",
        "http://short.test:8443/abc1234",
    ],
)
def test_every_spelling_of_our_own_host_is_refused(
    db_client: TestClient, owner_id: int, own_link: str
) -> None:
    response = create(db_client, original_url=own_link)

    assert response.status_code == 422
    assert error_code(response) == "redirect_loop"


class TestExtraOwnHosts:
    """OWN_HOSTS: more host names this shortener answers on (e.g. a short domain)."""

    @pytest.fixture
    def test_settings(self, test_settings: Settings) -> Iterator[Settings]:
        # A comma-separated text, exactly as it would come from the environment
        data = {**test_settings.model_dump(), "own_hosts": "sho.rt, WWW.Short.Test."}
        yield Settings(**data)

    @pytest.mark.parametrize(
        "own_link", ["https://sho.rt/abc", "https://www.short.test/abc", "http://SHO.RT./x"]
    )
    def test_the_extra_hosts_are_refused(
        self, db_client: TestClient, owner_id: int, own_link: str
    ) -> None:
        response = create(db_client, original_url=own_link)

        assert response.status_code == 422
        assert error_code(response) == "redirect_loop"

    def test_other_hosts_still_work(self, db_client: TestClient, owner_id: int) -> None:
        # Only exact host names are ours: a look-alike or a sub-domain is someone else's
        for url in ("https://sho.rt.example.org/", "https://blog.sho.rt/", BASE):
            assert create(db_client, original_url=url).status_code == 201, url


def test_own_hosts_are_read_from_a_comma_separated_list() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]  # ignore a local .env file
        base_url="http://localhost:8000",
        own_hosts=" sho.rt , WWW.Example.COM., ,",
    )

    assert settings.own_hosts == ["sho.rt", "www.example.com"]
    assert settings.own_host_names == frozenset({"localhost", "sho.rt", "www.example.com"})


def test_own_hosts_are_empty_by_default() -> None:
    assert Settings(_env_file=None).own_hosts == []  # type: ignore[call-arg]


# ---------- custom aliases ----------


@pytest.mark.parametrize(
    "alias",
    ["api", "docs", "redoc", "openapi.json", "admin", "health", "static", "API", "Docs"],
)
def test_reserved_aliases_are_refused(db_client: TestClient, owner_id: int, alias: str) -> None:
    response = create(db_client, original_url=BASE, custom_alias=alias)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "alias",
    [
        "ab",  # too short
        "a" * 11,  # too long
        "my link",
        "my.link",
        "my/link",
        "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
        "link\n",  # a hidden line break at the end
        "../etc",
        "%2e%2e",
    ],
)
def test_badly_formed_aliases_are_refused(db_client: TestClient, owner_id: int, alias: str) -> None:
    assert create(db_client, original_url=BASE, custom_alias=alias).status_code == 422


@pytest.mark.parametrize("alias", ["abc", "a" * 10, "My_Link-1"])
def test_well_formed_aliases_are_accepted(db_client: TestClient, owner_id: int, alias: str) -> None:
    response = create(db_client, original_url=BASE, custom_alias=alias)

    assert response.status_code == 201
    assert response.json()["short_code"] == alias


def test_a_taken_alias_is_409(db_client: TestClient, owner_id: int) -> None:
    assert create(db_client, original_url=BASE, custom_alias="taken").status_code == 201

    response = create(db_client, original_url=BASE, custom_alias="taken")

    assert response.status_code == 409
    assert error_code(response) == "alias_taken"


@pytest.mark.parametrize(
    ("host", "normalized"),
    [
        ("Short.Test.", "short.test"),
        ("B\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.de", "xn--bcher-kva.de"),  # as HttpUrl does
        ("a" * 64 + ".com", "a" * 64 + ".com"),  # too long to convert: kept as typed
    ],
)
def test_hosts_are_normalized_the_way_urls_are(host: str, normalized: str) -> None:
    assert normalize_host(host) == normalized


def test_own_hosts_must_be_text_or_a_list() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, own_hosts=5)  # type: ignore[call-arg,arg-type]
