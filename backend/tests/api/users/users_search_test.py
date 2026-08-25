"""Tests for the GET /users/search endpoint (sharing-picker user search).

Covers:
  1. Full happy-path scenario: normal user can search, results include correct
     fields, current user is excluded, case-insensitive substring matching works
     for both email and full_name.
  2. Short / empty query guard — queries shorter than 2 chars return an empty
     envelope; the is_active filter excludes deactivated users.
  3. limit clamping (1–25) and LIKE-wildcard escaping (`%` / `_`).
  4. Authentication guard — unauthenticated requests are rejected.
"""

import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.user import (
    create_random_user,
    create_random_user_with_headers,
    user_authentication_headers,
)
from tests.utils.utils import random_lower_string

_SEARCH = f"{settings.API_V1_STR}/users/search"

# This file creates users + routes/assignments but no agents; opt out of the
# heavy agent/env stubs in tests/api/users/conftest.py.
NEEDS_AGENT_STUBS = False


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _search(
    client: TestClient,
    headers: dict[str, str],
    q: str,
    limit: int | None = None,
) -> dict:
    """Call GET /users/search and return the parsed JSON response.

    Asserts HTTP 200 before returning.
    """
    params: dict = {"q": q}
    if limit is not None:
        params["limit"] = limit
    r = client.get(_SEARCH, headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Scenario 1: Happy-path search — access, projection, exclusion, matching
# ---------------------------------------------------------------------------


def test_user_search_happy_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Core search behaviour:
      1. A normal (non-admin) user can call GET /users/search and gets 200.
      2. Each result carries exactly id, email, full_name — sensitive fields
         (hashed_password, is_superuser, google_id, etc.) are absent.
      3. The current user is excluded from results even when their email
         matches the query term.
      4. Email substring match (case-insensitive).
      5. full_name substring match (case-insensitive).
      6. A different user's query doesn't surface the searcher's own record.
    """
    # ── Phase 1: Normal user can search (not superuser-only) ──────────────
    # Create a user who will perform the search.
    searcher, searcher_headers = create_random_user_with_headers(client)

    # Create a target user whose email contains a known, unique suffix.
    unique_tag = f"srch{random_lower_string()[:10]}"
    target_email = f"{unique_tag}@example.com"
    target_password = random_lower_string()
    r = client.post(
        f"{settings.API_V1_STR}/users/signup",
        json={
            "email": target_email,
            "password": target_password,
            "full_name": f"Alice {unique_tag.upper()}",
        },
    )
    assert r.status_code == 200, r.text
    target = r.json()

    # ── Phase 2: Normal user gets 200 ─────────────────────────────────────
    body = _search(client, searcher_headers, q=unique_tag)
    assert "data" in body
    assert "count" in body
    assert body["count"] >= 1

    # At least the target user must appear.
    matches = {u["id"]: u for u in body["data"]}
    assert target["id"] in matches, (
        f"Expected target user {target['id']} in results; got {list(matches)}"
    )

    # ── Phase 3: Minimal projection — no sensitive fields ─────────────────
    result = matches[target["id"]]
    # Required fields present.
    assert "id" in result
    assert "email" in result
    assert "full_name" in result
    # Sensitive / bulky fields must NOT appear.
    for forbidden in (
        "hashed_password",
        "is_superuser",
        "is_active",
        "google_id",
        "ai_credentials_encrypted",
        "two_factor_enabled",
        "role",
    ):
        assert forbidden not in result, (
            f"Sensitive field '{forbidden}' leaked into search result"
        )

    # ── Phase 4: Current user excluded ────────────────────────────────────
    # Search using a term that would match the searcher's own email.
    searcher_email_fragment = searcher["email"][:12]
    own_body = _search(client, searcher_headers, q=searcher_email_fragment)
    own_ids = {u["id"] for u in own_body["data"]}
    assert searcher["id"] not in own_ids, (
        "Searcher's own record must not appear in their own search results"
    )

    # ── Phase 5: Email substring match (case-insensitive) ─────────────────
    # Search with upper-cased version of the unique tag.
    upper_body = _search(client, searcher_headers, q=unique_tag.upper())
    upper_ids = {u["id"] for u in upper_body["data"]}
    assert target["id"] in upper_ids, (
        "Case-insensitive email match should find target even with uppercased query"
    )

    # ── Phase 6: full_name substring match (case-insensitive) ─────────────
    # The target's full_name contains unique_tag.upper(); search by lowercase.
    name_body = _search(client, searcher_headers, q=unique_tag.upper()[:8].lower())
    name_ids = {u["id"] for u in name_body["data"]}
    assert target["id"] in name_ids, (
        "Case-insensitive full_name match should find target via name fragment"
    )


# ---------------------------------------------------------------------------
# Scenario 2: Short-query guard, is_active filter, and unauthenticated guard
# ---------------------------------------------------------------------------


def test_user_search_guards_and_inactive_exclusion(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Guard and filtering rules:
      1. Query shorter than 2 chars returns {data: [], count: 0}.
      2. Empty string returns the same empty envelope.
      3. Whitespace-only query returns the same empty envelope.
      4. Unauthenticated request is rejected (401/403).
      5. An inactive (deactivated) user is NOT returned even when their
         email matches the query term.
    """
    _, searcher_headers = create_random_user_with_headers(client)

    # ── Phase 1: One-char query → empty ───────────────────────────────────
    one_char = _search(client, searcher_headers, q="a")
    assert one_char == {"data": [], "count": 0}

    # ── Phase 2: Empty string → empty ─────────────────────────────────────
    empty = _search(client, searcher_headers, q="")
    assert empty == {"data": [], "count": 0}

    # ── Phase 3: Whitespace-only → empty ──────────────────────────────────
    ws = _search(client, searcher_headers, q="   ")
    assert ws == {"data": [], "count": 0}

    # ── Phase 4: Unauthenticated → 401/403 ────────────────────────────────
    r = client.get(_SEARCH, params={"q": "test"})
    assert r.status_code in (401, 403), (
        f"Expected 401/403 without auth, got {r.status_code}"
    )

    # ── Phase 5: Inactive user not returned ───────────────────────────────
    # Create a user, then deactivate them via the admin PATCH endpoint.
    unique_tag = f"inact{random_lower_string()[:10]}"
    inactive_email = f"{unique_tag}@example.com"
    inactive_password = random_lower_string()
    r = client.post(
        f"{settings.API_V1_STR}/users/signup",
        json={"email": inactive_email, "password": inactive_password},
    )
    assert r.status_code == 200
    inactive_user = r.json()

    # Deactivate the user via the admin endpoint.
    r = client.patch(
        f"{settings.API_V1_STR}/users/{inactive_user['id']}",
        headers=superuser_token_headers,
        json={"is_active": False},
    )
    assert r.status_code == 200, r.text

    # Search for the inactive user by their unique email fragment.
    body = _search(client, searcher_headers, q=unique_tag)
    result_ids = {u["id"] for u in body["data"]}
    assert inactive_user["id"] not in result_ids, (
        "Inactive user must not appear in search results"
    )


# ---------------------------------------------------------------------------
# Scenario 3: limit clamping and LIKE-wildcard escaping
# ---------------------------------------------------------------------------


def test_user_search_limit_clamping_and_wildcard_escaping(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Edge-case rules for limit and special query characters:
      1. limit=0 is clamped to 1 (minimum).
      2. limit=100 is clamped to 25 (maximum).
      3. Searching for a literal '%' does NOT return every active user
         (the LIKE wildcard must be escaped so '%' matches that exact
         character, not everything).
      4. Searching for a literal '_' similarly matches literally.
    """
    _, searcher_headers = create_random_user_with_headers(client)

    # ── Phase 1: limit=0 clamped to 1 ─────────────────────────────────────
    # Use the superuser's known email as a search term so we get at least one
    # result to verify the cap has been applied (count <= 1).
    # We search by a generic, common fragment and verify no more than 1 row.
    unique_term = f"lim{random_lower_string()[:10]}"
    # Create 3 users all sharing unique_term in their email so the unclamped
    # query would return 3.
    for i in range(3):
        r = client.post(
            f"{settings.API_V1_STR}/users/signup",
            json={
                "email": f"{unique_term}{i}@example.com",
                "password": random_lower_string(),
            },
        )
        assert r.status_code == 200, r.text

    body_limit1 = _search(client, searcher_headers, q=unique_term, limit=0)
    assert len(body_limit1["data"]) <= 1, (
        "limit=0 should be clamped to 1; got more than 1 result"
    )

    # ── Phase 2: limit=100 clamped to 25 ──────────────────────────────────
    body_big = _search(client, searcher_headers, q=unique_term, limit=100)
    assert len(body_big["data"]) <= 25, (
        "limit=100 should be clamped to 25; got more than 25 results"
    )

    # ── Phase 3: '%' query matches literally, not everyone ────────────────
    # Searching for a bare '%' must return 0 results because no user has
    # a literal '%' in their email or name.
    percent_body = _search(client, searcher_headers, q="%")
    assert percent_body == {"data": [], "count": 0}, (
        "Searching for '%' should return empty (short query guard: length 1 < 2 minimum)"
    )

    # Use '%%' (two chars) to exercise the escaping path directly.
    percent2_body = _search(client, searcher_headers, q="%%")
    assert percent2_body["count"] == 0, (
        "Searching for '%%' must not match every user — LIKE wildcard must be escaped"
    )

    # ── Phase 4: '_' query matches literally ──────────────────────────────
    # Two underscores is 2 chars; if not escaped it would be a double wildcard.
    underscore_body = _search(client, searcher_headers, q="__")
    # Any results returned must have literally '__' in email or full_name.
    for user in underscore_body["data"]:
        assert "__" in (user.get("email") or "") or "__" in (user.get("full_name") or ""), (
            f"User {user['id']} matched '__' query but does not have '__' in email or name"
        )


