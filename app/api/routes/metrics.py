"""GET /metrics: the numbers Prometheus scrapes (not part of the public API docs)."""

import hmac

import anyio
from fastapi import APIRouter, Request, Response

from app.api.dependencies import SettingsDep
from app.core.exceptions import NotAuthenticatedError
from app.core.metrics import render_metrics

router = APIRouter(tags=["Monitoring"])


def check_scrape_token(request: Request, expected: str) -> None:
    """Require "Authorization: Bearer <METRICS_TOKEN>" when a token is configured.

    Raises:
        NotAuthenticatedError: If the header is missing or holds another token (401).
    """
    sent = request.headers.get("authorization", "")
    # compare_digest takes the same time whatever the input: the response time
    # cannot tell an attacker how many characters were right
    if not hmac.compare_digest(sent.encode(), f"Bearer {expected}".encode()):
        raise NotAuthenticatedError("Send 'Authorization: Bearer <METRICS_TOKEN>' to read metrics.")


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, settings: SettingsDep) -> Response:
    """Return every metric in Prometheus' text format."""
    if settings.metrics_token is not None:
        check_scrape_token(request, settings.metrics_token.get_secret_value())
    # Reading the multiprocess files is disk I/O: done in a thread, not on the event loop
    body, content_type = await anyio.to_thread.run_sync(render_metrics)
    return Response(body, media_type=content_type, headers={"Cache-Control": "no-store"})
