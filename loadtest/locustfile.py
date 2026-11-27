"""Load test for the redirect (Locust), run from Locust's Docker image (no local install).

    SHORT_CODES=abc1234,xyz9876 docker compose --profile loadtest run --rm locust

Each simulated visitor waits 0.1-0.5 s between actions and mostly opens popular links
(cache hits), sometimes an unknown code (cache miss -> database -> 404, never cached),
and now and then the readiness probe. Locust reports requests per second and the
p50 / p95 / p99 response times per kind of request.
"""

import os
import random
import string

from locust import HttpUser, between, task
from locust.exception import StopUser

# Live short codes to visit (comma-separated): the "popular links"
HOT_CODES = [code for code in os.environ.get("SHORT_CODES", "").split(",") if code]


class Visitor(HttpUser):
    """One simulated visitor."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        """Refuse to run without codes: every request would be a 404."""
        if not HOT_CODES:
            raise StopUser("Set SHORT_CODES to one or more live short codes")

    @task(8)
    def popular_link(self) -> None:
        """Open a popular link: answered from the Redis cache."""
        # allow_redirects=False: measure OUR 302, never load the target website
        self.client.get(
            f"/{random.choice(HOT_CODES)}", allow_redirects=False, name="/{short_code} hit"
        )

    @task(2)
    def unknown_link(self) -> None:
        """Open a code that does not exist: the database answers 404 every time."""
        code = "".join(random.choices(string.ascii_letters + string.digits, k=7))
        with self.client.get(
            f"/{code}", allow_redirects=False, name="/{short_code} miss (404)", catch_response=True
        ) as response:
            if response.status_code == 404:
                response.success()  # the expected answer, not an error

    @task(1)
    def readiness(self) -> None:
        """The probe a load balancer would send."""
        self.client.get("/health/ready", name="/health/ready")
