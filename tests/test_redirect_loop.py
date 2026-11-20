"""An original URL may not point back to this shortener: the link would loop forever.

The test app's BASE_URL is http://short.test/ (see conftest.py).
"""

import pytest
from fastapi.testclient import TestClient

OTHER_SITE = "https://www.python.org/"


@pytest.mark.parametrize(
    "own_link",
    [
        "http://short.test/abc1234",  # a short link of ours
        "https://short.test/",  # another scheme, same host
        "http://SHORT.test:9000/x",  # letter case and port do not matter
    ],
)
def test_create_refuses_a_link_to_ourselves(
    db_client: TestClient, owner_id: int, own_link: str
) -> None:
    response = db_client.post("/api/v1/urls/", json={"original_url": own_link})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "redirect_loop"
    assert "original_url" in error["details"]


def test_update_refuses_a_link_to_ourselves_and_keeps_the_old_one(
    db_client: TestClient, owner_id: int
) -> None:
    code = db_client.post("/api/v1/urls/", json={"original_url": OTHER_SITE}).json()["short_code"]

    # The classic loop: the link pointing at its own short URL
    response = db_client.put(
        f"/api/v1/urls/{code}/", json={"original_url": f"http://short.test/{code}"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "redirect_loop"
    assert db_client.get(f"/api/v1/urls/{code}/").json()["original_url"] == OTHER_SITE


def test_other_sites_are_still_allowed(db_client: TestClient, owner_id: int) -> None:
    code = db_client.post("/api/v1/urls/", json={"original_url": OTHER_SITE}).json()["short_code"]

    response = db_client.put(
        f"/api/v1/urls/{code}/", json={"original_url": "https://short.test.example.org/"}
    )

    assert response.status_code == 200  # a different host that only LOOKS similar
