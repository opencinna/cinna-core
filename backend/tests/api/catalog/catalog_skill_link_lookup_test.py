"""Skill links — ``GET /skills/packages/by-package-id/{package_id}``.

The lookup behind the public ``/get/skill/<package_id>`` page. It answers with
the same detail and the same access rule as ``GET /skills/packages/{uuid}``,
and a missing package reads exactly like one the caller may not see.

Scenarios:
  1. Found: the publisher and (once public) another user get the same
     detail the uuid route returns.
  2. Not visible → 404 ``package_not_found``, byte-for-byte the missing answer.
  3. Missing → 404; unauthenticated → 401.
  4. Malformed ids (characters outside ``[A-Za-z0-9._-]``, over 255) → 422.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person, publish_skill_package
from tests.utils.skill_catalog import (
    error_code,
    get_skill_package,
    get_skill_package_by_package_id,
    update_skill_package,
)

API = settings.API_V1_STR


def test_lookup_by_package_id_follows_package_visibility(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Publisher publishes a private skill; looks it up by package_id → same
         detail as the uuid route
      2. Stranger: private → 404, identical to a missing id
      3. Publisher makes it public → stranger finds it
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    stranger = make_person(client, su)
    pkg = publish_skill_package(client, publisher.headers, "link-skill")

    # ── Phase 1: found ────────────────────────────────────────────────
    by_id = get_skill_package_by_package_id(
        client, publisher.headers, pkg["package_id"]
    )
    by_uuid = get_skill_package(client, publisher.headers, pkg["package_uuid"])
    assert by_id["id"] == pkg["package_uuid"]
    assert by_id["package_id"] == pkg["package_id"]
    assert by_id == by_uuid

    # ── Phase 2: not visible reads as missing ─────────────────────────
    hidden = get_skill_package_by_package_id(
        client, stranger.headers, pkg["package_id"], expected_status=404
    )
    missing = get_skill_package_by_package_id(
        client, stranger.headers, "io.example.nobody.skill.nothing",
        expected_status=404,
    )
    assert error_code(hidden) == "package_not_found"
    assert hidden == missing

    # ── Phase 3: public ───────────────────────────────────────────────
    update_skill_package(
        client, publisher.headers, pkg["package_uuid"], visibility="public"
    )
    found = get_skill_package_by_package_id(
        client, stranger.headers, pkg["package_id"]
    )
    assert found["id"] == pkg["package_uuid"]


def test_lookup_by_package_id_rejects_malformed_and_anonymous(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Unauthenticated → 401
      2. Ids outside ``[A-Za-z0-9._-]{1,255}`` → 422
      3. A missing well-formed id with ``_`` → 404 (the link grammar allows it)
    """
    person = make_person(client, superuser_token_headers)

    r = client.get(f"{API}/skills/packages/by-package-id/io.example.skill.x")
    assert r.status_code == 401, r.text

    for bad in ("io.example%20skill", "io.example~skill", "a" * 256):
        get_skill_package_by_package_id(
            client, person.headers, bad, expected_status=422
        )

    get_skill_package_by_package_id(
        client, person.headers, "io.example.under_score", expected_status=404
    )
