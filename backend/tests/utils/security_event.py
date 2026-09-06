"""Reading a security feed through the API.

``GET /security-events/`` is **self-scoped** — it answers with the calling
user's own rows and nobody else's (the route's docstring explains why, and
another test depends on it). That is what makes it usable as an assertion
about attribution: to claim an event landed in a particular person's feed you
have to hold that person's token, and an event written into the wrong feed is
simply absent from the one you are reading.

Both directions matter to callers here, so the helper returns the list rather
than asserting anything about it: a feed that *contains* the row proves the
event was attributed to that user, and a feed that does not contain it proves
it was not.
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR


def events_of_type(
    client: TestClient,
    headers: dict[str, str],
    event_type: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Every event of ``event_type`` in the calling user's own feed."""
    response = client.get(
        f"{API}/security-events/",
        headers=headers,
        params={"event_type": event_type, "limit": limit},
    )
    assert response.status_code == 200, response.text
    return list(response.json()["data"])
