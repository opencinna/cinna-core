"""Brand theme API: admin drafts, publish/unpublish, the public endpoint,
assets and the discovery field.

Validator rules are covered rule-by-rule against the shared fixtures in
``tests/unit/test_brand_theme_fixtures.py``; here they are exercised only
through the upload and publish paths.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.utils.brand_theme import (
    ADMIN_URL,
    DISCOVERY_URL,
    PUBLIC_URL,
    delete_draft,
    fixture_files,
    fixture_zip,
    list_drafts,
    publish,
    theme_with,
    unpublish,
    upload_files,
    upload_zip,
)


def _brand_events(stub) -> list[dict]:
    return [e["data"] for e in stub.emitted_events if e["data"].get("type") == "brand_theme.updated"]


def test_no_theme_means_204_and_no_discovery_field(client: TestClient) -> None:
    r = client.get(PUBLIC_URL)
    assert r.status_code == 204
    assert r.headers["cache-control"] == "no-cache"
    assert "brand_theme" not in client.get(DISCOVERY_URL).json()


def test_admin_routes_are_superuser_only(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    assert client.get(f"{ADMIN_URL}/drafts").status_code == 401
    assert client.get(f"{ADMIN_URL}/drafts", headers=normal_user_token_headers).status_code == 403
    r = upload_zip(client, normal_user_token_headers, fixture_zip("valid/minimal"))
    assert r.status_code == 403
    assert client.post(f"{ADMIN_URL}/unpublish", headers=normal_user_token_headers).status_code == 403


def test_invalid_package_is_refused_with_errors_and_never_saved(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    before = list_drafts(client, superuser_token_headers)["count"]

    r = upload_zip(client, superuser_token_headers, fixture_zip("invalid/low-contrast-text"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] is None
    assert body["errors"] == [{
        "path": "modes.dark.text.muted",
        "code": "low_contrast",
        "message": body["errors"][0]["message"],
    }]
    assert "text.muted #333333 on surface.base #0f0f0f" in body["errors"][0]["message"]

    # An unsafe SVG payload that cannot be sanitised is refused the same way.
    r = upload_zip(client, superuser_token_headers, fixture_zip("invalid/svg-entity-expansion"))
    assert {e["code"] for e in r.json()["errors"]} == {"svg_entities"}

    # Not a zip at all: a package-level error, still not saved.
    r = client.post(
        f"{ADMIN_URL}/drafts",
        headers=superuser_token_headers,
        files={"package": ("theme.zip", b"nope", "application/zip")},
    )
    assert r.json()["id"] is None and r.json()["errors"][0]["path"] == "package"

    # Both or neither of package/theme is a request error.
    r = client.post(f"{ADMIN_URL}/drafts", headers=superuser_token_headers, files={"files": ("a.svg", b"x")})
    assert r.status_code == 400

    assert list_drafts(client, superuser_token_headers)["count"] == before


def test_publish_lifecycle_etag_assets_discovery_and_events(
    client: TestClient, superuser_token_headers: dict[str, str], socket_stub
) -> None:
    su = superuser_token_headers

    # ── upload the full fixture as a zipped folder ─────────────────────
    r = upload_zip(client, su, fixture_zip("valid/full", folder="acme"))
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["id"] and first["errors"] == []
    # The logo's XML declaration and comment are dropped without a warning
    # (warnings name removed elements and attributes only).
    assert first["warnings"] == []
    draft = client.get(f"{ADMIN_URL}/drafts/{first['id']}", headers=su).json()
    assert draft["status"] == "draft" and draft["revision"] is None
    assert draft["name"] == "Acme"
    assert draft["created_by_email"]
    assert set(draft["document"]["assets"]) == {"logo.dark", "logo.light", "background.dark", "background.light"}

    # Drafts are not public.
    assert client.get(PUBLIC_URL).status_code == 204

    # ── publish ────────────────────────────────────────────────────────
    r = publish(client, su, first["id"])
    assert r.status_code == 200, r.text
    published = r.json()
    assert published["status"] == "published"
    rev1 = published["revision"]
    assert isinstance(rev1, int) and rev1 > 0
    assert published["published_by_email"] == draft["created_by_email"]
    assert _brand_events(socket_stub)[-1]["meta"] == {"published": True, "revision": rev1}

    # ── public read, ETag and 304 ──────────────────────────────────────
    r = client.get(PUBLIC_URL)
    assert r.status_code == 200
    assert r.headers["etag"] == f'"{rev1}"'
    assert r.headers["cache-control"] == "no-cache"
    doc = r.json()
    assert doc["schema"] == "cinna.brand-theme/1" and doc["revision"] == rev1
    theme = doc["theme"]
    assert theme["copy"] == {"newChatHeading": "What can Acme AI help with?"}
    assert all(v.startswith("#") and len(v) == 7 for v in theme["modes"]["dark"].values())
    assert theme["logo"]["dark"] == doc["assets"]["logo.dark"]
    assert theme["logo"]["dark"]["type"] == "image/svg+xml" and "width" not in theme["logo"]["dark"]
    assert theme["logo"]["light"]["type"] == "image/png"
    assert (theme["logo"]["light"]["width"], theme["logo"]["light"]["height"]) == (480, 124)

    r = client.get(PUBLIC_URL, headers={"If-None-Match": f'"{rev1}"'})
    assert r.status_code == 304 and r.content == b""
    assert client.get(PUBLIC_URL, headers={"If-None-Match": '"0"'}).status_code == 200

    # ── assets: immutable, content-addressed, sanitised SVG ────────────
    svg_ref = doc["assets"]["logo.dark"]
    r = client.get(svg_ref["url"])
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert hashlib.sha256(r.content).hexdigest() == svg_ref["sha256"] == svg_ref["url"].rsplit("/", 1)[1]
    assert len(r.content) == svg_ref["bytes"]
    assert b"<?xml" not in r.content and b"<!--" not in r.content
    _, package_assets = fixture_files("valid/full")
    assert r.content != package_assets["assets/logo-dark.svg"], "the upload is never what is served"

    r = client.get(doc["assets"]["background.dark"]["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "content-security-policy" not in r.headers
    assert client.get(f"{PUBLIC_URL}/assets/{'0' * 64}").status_code == 404
    assert client.get(f"{PUBLIC_URL}/assets/not-a-hash").status_code == 422

    # ── discovery advertises the theme, never its revision ─────────────
    disco = client.get(DISCOVERY_URL).json()
    assert disco["brand_theme"]["schema"] == 1
    assert disco["brand_theme"]["url"].endswith("/api/v1/brand-theme")
    assert "revision" not in disco["brand_theme"]

    # ── a second theme (loose files) replaces the first ────────────────
    theme_json, assets = fixture_files("valid/background-light-null")
    r = upload_files(client, su, theme_with("valid/background-light-null", name="Acme Two"), assets)
    second = r.json()
    assert second["id"], r.text
    # Re-publishing the live theme is a no-op: same revision, no event.
    events_before = len(_brand_events(socket_stub))
    r = publish(client, su, first["id"])
    assert r.json()["revision"] == rev1
    assert len(_brand_events(socket_stub)) == events_before

    r = publish(client, su, second["id"])
    rev2 = r.json()["revision"]
    assert rev2 > rev1
    statuses = {d["id"]: d["status"] for d in list_drafts(client, su)["data"]}
    assert statuses[first["id"]] == "draft" and statuses[second["id"]] == "published"
    r = client.get(PUBLIC_URL, headers={"If-None-Match": f'"{rev1}"'})
    assert r.status_code == 200 and r.json()["theme"]["name"] == "Acme Two"
    assert r.json()["theme"]["background"]["light"] is None
    assert "background.light" not in r.json()["assets"]

    # A published theme cannot be deleted.
    assert delete_draft(client, su, second["id"]).status_code == 409

    # ── unpublish: back to stock everywhere ────────────────────────────
    unpublish(client, su)
    assert client.get(PUBLIC_URL).status_code == 204
    assert client.get(PUBLIC_URL, headers={"If-None-Match": f'"{rev2}"'}).status_code == 204
    assert "brand_theme" not in client.get(DISCOVERY_URL).json()
    assert _brand_events(socket_stub)[-1]["meta"] == {"published": False, "revision": None}
    # Idempotent, and quiet when nothing changed.
    events_before = len(_brand_events(socket_stub))
    unpublish(client, su)
    assert len(_brand_events(socket_stub)) == events_before

    # Re-publishing the old draft gets a fresh revision, so no stale 304.
    r = publish(client, su, first["id"])
    assert r.json()["revision"] > rev2
    unpublish(client, su)

    # ── deleting drafts drops assets nobody else uses ──────────────────
    assert delete_draft(client, su, first["id"]).status_code == 204
    assert client.get(svg_ref["url"]).status_code == 404
    assert client.get(f"{ADMIN_URL}/drafts/{first['id']}", headers=su).status_code == 404
    assert delete_draft(client, su, second["id"]).status_code == 204


def test_publish_re_runs_the_validator(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    theme_id = upload_zip(client, su, fixture_zip("valid/minimal")).json()["id"]
    assert theme_id

    # The rules tighten after the upload: every text pair now needs 21:1.
    with patch("app.services.brand_theme.schema.CONTRAST_TEXT", 21.0):
        r = publish(client, su, theme_id)
    assert r.status_code == 409
    errors = r.json()["errors"]
    assert errors and {e["code"] for e in errors} == {"low_contrast"}
    assert all(e["path"].startswith("modes.") for e in errors)
    assert client.get(PUBLIC_URL).status_code == 204

    r = publish(client, su, theme_id)
    assert r.status_code == 200
    unpublish(client, su)
    assert delete_draft(client, su, theme_id).status_code == 204


def test_unknown_keys_are_refused_on_upload(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    theme, _ = fixture_files("valid/minimal")
    doc = json.loads(theme)
    doc["modes"]["dark"]["acent"] = "#ff0000"
    r = upload_files(client, superuser_token_headers, json.dumps(doc).encode(), {})
    assert r.json()["id"] is None
    assert [(e["path"], e["code"]) for e in r.json()["errors"]] == [("modes.dark.acent", "unknown_key")]


def test_public_reads_are_rate_limited(client: TestClient) -> None:
    from app.core.config import settings

    with patch.object(settings, "BRAND_THEME_RATE_LIMIT_PER_MIN", 2):
        assert client.get(PUBLIC_URL).status_code == 204
        assert client.get(f"{PUBLIC_URL}/assets/{'0' * 64}").status_code == 404
        r = client.get(PUBLIC_URL)
    assert r.status_code == 429 and "retry-after" in r.headers


def test_an_asset_shared_by_two_drafts_survives_deleting_one(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    theme_json, assets = fixture_files("valid/background-light-null")
    a = upload_files(client, su, theme_json, assets).json()["id"]
    b = upload_files(client, su, theme_with("valid/background-light-null", name="Other"), assets).json()["id"]
    url = client.get(f"{ADMIN_URL}/drafts/{a}", headers=su).json()["document"]["assets"]["background.dark"]["url"]

    assert delete_draft(client, su, a).status_code == 204
    assert client.get(url).status_code == 200
    assert delete_draft(client, su, b).status_code == 204
    assert client.get(url).status_code == 404


def test_loose_upload_limits(client: TestClient, superuser_token_headers: dict[str, str]) -> None:
    su = superuser_token_headers
    theme_json, _ = fixture_files("valid/minimal")

    # theme.json over its cap is a request-size error.
    r = upload_files(client, su, b" " * (256 * 1024 + 1), {})
    assert r.status_code == 413

    # Two files with one name are refused, not silently merged.
    files = [
        ("theme", ("theme.json", theme_json, "application/json")),
        ("files", ("logo.svg", b"<svg/>", "image/svg+xml")),
        ("files", ("logo.svg", b"<svg/>", "image/svg+xml")),
    ]
    r = client.post(f"{ADMIN_URL}/drafts", headers=su, files=files)
    assert r.status_code == 200 and r.json()["id"] is None
    assert r.json()["errors"][0]["code"] == "invalid_package"

    too_many = [("theme", ("theme.json", theme_json, "application/json"))]
    too_many += [("files", (f"f{i}.svg", b"<svg/>", "image/svg+xml")) for i in range(65)]
    assert client.post(f"{ADMIN_URL}/drafts", headers=su, files=too_many).status_code == 413


def test_publish_stores_what_todays_sanitiser_produces(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A sanitiser tightened after upload re-sanitises the logo at publish:
    the served bytes (and so the content hash) are the new output."""
    su = superuser_token_headers
    theme_id = upload_zip(client, su, fixture_zip("valid/full")).json()["id"]
    before = client.get(f"{ADMIN_URL}/drafts/{theme_id}", headers=su).json()["document"]["assets"]["logo.dark"]

    # Every attribute the fixture logo uses except stroke-linecap.
    tightened = frozenset({
        "viewBox", "width", "height", "id", "x1", "y1", "x2", "y2", "offset",
        "stop-color", "fill", "stroke", "stroke-width", "d",
    })
    with patch("app.services.brand_theme.svg_sanitizer.ALLOWED_ATTRIBUTES", tightened):
        r = publish(client, su, theme_id)
    assert r.status_code == 200, r.text
    after = r.json()["document"]["assets"]["logo.dark"]
    assert after["sha256"] != before["sha256"]
    served = client.get(PUBLIC_URL).json()["assets"]["logo.dark"]
    assert served == after
    body = client.get(after["url"]).content
    assert b"stroke-linecap" not in body and hashlib.sha256(body).hexdigest() == after["sha256"]
    # The superseded bytes are gone once nothing references them.
    assert client.get(before["url"]).status_code == 404

    unpublish(client, su)
    assert delete_draft(client, su, theme_id).status_code == 204
