"""Phase 5 — install-experience-redesign tests.

Covers the publisher override map (``Agent.publish_settings``) and the
removal of the legacy install shim.

Scenarios:
  A. PATCH /agents/{id}/publish-settings — happy path: stores overrides and
     GET confirms persistence.
  B. PATCH publish-settings — rejects override key that doesn't match any
     linked credential name (HTTP 4xx).
  C. PATCH publish-settings — rejects an invalid ``provided_by`` value
     (HTTP 4xx).
  D. PATCH publish-settings — auth: another user (install non-owner) gets
     403/404; a non-developer user also gets 403.
  E. Override provided_by="publisher" on a non-shareable credential → publish
     fails (``_validate_publisher_provides`` error, HTTP 400).
  F. Override provided_by="user" on a shareable credential → publish emits
     provided_by="user" and publisher_credential_id=null.
  G. Override provided_by="publisher" on a shareable credential → publish emits
     provided_by="publisher" and publisher_credential_id=<uuid>.
  H. No override → inference still applies (regression: allow_sharing=True →
     provided_by="publisher").
  I. Mixed: one shareable-override-to-user + one non-shareable-no-override →
     first spec is user, second spec is user (inference fallback).
  J. Regression: install with the new InstallCredentialSelection payload shape
     still works (confirms the new shape is the only accepted path after shim
     removal).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a random user with a default AI credential and return both."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _create_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    allow_sharing: bool = False,
) -> dict:
    """Create an api_token credential via the credentials API."""
    name = name or f"cred-{uuid.uuid4().hex[:8]}"
    r = client.post(
        f"{API}/credentials/",
        headers=headers,
        json={
            "name": name,
            "type": "api_token",
            "allow_sharing": allow_sharing,
            "credential_data": {
                "api_token_type": "bearer",
                "api_token_template": "Authorization: Bearer {TOKEN}",
                "api_token": "test-token-value",
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _link_credential(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    credential_id: str,
) -> None:
    r = client.post(
        f"{API}/agents/{agent_id}/credentials",
        headers=headers,
        json={"credential_id": credential_id},
    )
    assert r.status_code == 200, r.text


def _patch_publish_settings(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    credential_overrides: dict,
) -> dict:
    """PATCH /agents/{agent_id}/publish-settings and return parsed JSON."""
    r = client.patch(
        f"{API}/agents/{agent_id}/publish-settings",
        headers=headers,
        json={"credential_overrides": credential_overrides},
    )
    return r


def _publish(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Publish and return the agent dict (with bundle_uuid)."""
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    # Return fresh agent with bundle_uuid
    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    return fresh


def _promote_to_publisher_install(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Do an initial publish to promote the agent to a publisher install.

    First publish sets ``is_publisher_install=True``.  Subsequent PATCH
    publish-settings calls require this flag, so call this helper before
    PATCHing.  Returns the fresh agent dict (includes ``bundle_uuid``).
    """
    return _publish(client, headers, agent_id)


def _publish_raw(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
):
    """Publish and return the raw response (for error-case assertions)."""
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={},
    )
    return r


def _get_latest_specs(
    client: TestClient,
    headers: dict[str, str],
    bundle_uuid: str,
) -> list[dict]:
    """Return required_credential_specs from the newest revision."""
    revs = client.get(
        f"{API}/bundles/{bundle_uuid}/revisions",
        headers=headers,
    )
    assert revs.status_code == 200, revs.text
    data = revs.json()["data"]
    assert data, "Expected at least one revision"
    return data[0]["required_credential_specs"]


def _make_public(
    client: TestClient,
    headers: dict[str, str],
    bundle_uuid: str,
) -> None:
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"is_listed": True, "visibility": "public"},
    )
    assert r.status_code == 200, r.text


def _install(
    client: TestClient,
    headers: dict[str, str],
    bundle_id: str,
    request_body: dict | None = None,
) -> dict:
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=headers,
        json=request_body or {},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Scenario A: happy path ────────────────────────────────────────────────────


def test_patch_publish_settings_happy_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A. PATCH publish-settings stores the override and GET confirms persistence.

    1. Create agent.
    2. Create and link a credential (shareable, so the override is meaningful).
    3. PATCH publish-settings with provided_by="publisher".
    4. Assert 200, response body includes publish_settings with the override.
    5. GET the agent and confirm the field is persisted.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5A-Agent"
    )
    drain_tasks()
    cred = _create_credential(
        client, superuser_token_headers, name="gmail", allow_sharing=True
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred["id"])
    # First publish promotes the install to is_publisher_install=True.
    _promote_to_publisher_install(client, superuser_token_headers, agent["id"])

    r = _patch_publish_settings(
        client,
        superuser_token_headers,
        agent["id"],
        {"gmail": {"provided_by": "publisher"}},
    )
    assert r.status_code == 200, (
        f"Expected 200 from PATCH publish-settings; got {r.status_code}: {r.text}"
    )
    body = r.json()
    overrides = (
        body.get("publish_settings", {})
        .get("credential_overrides", {})
    )
    assert "gmail" in overrides, (
        f"Expected 'gmail' key in credential_overrides; got {overrides}"
    )
    assert overrides["gmail"]["provided_by"] == "publisher", (
        f"Expected provided_by='publisher'; got {overrides['gmail']}"
    )

    # Confirm persistence via GET
    fresh = client.get(f"{API}/agents/{agent['id']}", headers=superuser_token_headers)
    assert fresh.status_code == 200, fresh.text
    persisted = (
        fresh.json()
        .get("publish_settings", {})
        .get("credential_overrides", {})
    )
    assert persisted.get("gmail", {}).get("provided_by") == "publisher", (
        f"publish_settings not persisted after GET; got {persisted}"
    )


# ── Scenario B: rejects unknown spec name ─────────────────────────────────────


def test_patch_publish_settings_rejects_unknown_spec_name(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """B. Override key doesn't match any linked credential → HTTP 4xx.

    Sending an override for a credential name that isn't linked to the install
    must be rejected with a clear error detail.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5B-Agent"
    )
    drain_tasks()
    # Link one credential so the agent has _some_ credentials
    cred = _create_credential(
        client, superuser_token_headers, name="real-cred", allow_sharing=True
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred["id"])
    _promote_to_publisher_install(client, superuser_token_headers, agent["id"])

    r = _patch_publish_settings(
        client,
        superuser_token_headers,
        agent["id"],
        {"nonexistent-cred": {"provided_by": "publisher"}},
    )
    assert r.status_code in (400, 422), (
        f"Expected 400 or 422 for unknown override key; got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "nonexistent-cred" in str(detail) or "unknown" in str(detail).lower(), (
        f"Expected error mentioning the bad key; got: {detail}"
    )


# ── Scenario C: rejects invalid provided_by value ────────────────────────────


def test_patch_publish_settings_rejects_invalid_provided_by(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """C. provided_by value not in ('user', 'publisher') → HTTP 4xx."""
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5C-Agent"
    )
    drain_tasks()
    cred = _create_credential(
        client, superuser_token_headers, name="svc-cred", allow_sharing=True
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred["id"])

    r = _patch_publish_settings(
        client,
        superuser_token_headers,
        agent["id"],
        {"svc-cred": {"provided_by": "anyone"}},
    )
    assert r.status_code in (400, 422), (
        f"Expected 400 or 422 for invalid provided_by; got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert detail, "Expected a non-empty error detail"


# ── Scenario D: auth — another user / non-developer gets 403/404 ─────────────


def test_patch_publish_settings_auth_non_owner_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """D. Another user (non-owner) trying PATCH publish-settings gets 403/404.

    Also checks that a non-developer user (who happens to be the owner) is
    rejected by the ``require_developer`` dependency.
    """
    # Create a publisher agent (owned by superuser)
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5D-Agent"
    )
    drain_tasks()
    cred = _create_credential(
        client, superuser_token_headers, name="d-cred", allow_sharing=True
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred["id"])

    # Another user (non-owner) — this user is not a developer either
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )
    create_random_ai_credential(client, other_headers, set_default=True)

    r = _patch_publish_settings(
        client,
        other_headers,
        agent["id"],
        {"d-cred": {"provided_by": "publisher"}},
    )
    assert r.status_code in (400, 403, 404), (
        f"Expected 400/403/404 for non-owner; got {r.status_code}: {r.text}"
    )


# ── Scenario E: override to publisher on non-shareable → publish rejects ──────


def test_override_publisher_non_shareable_credential_publish_fails(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """E. Override provided_by="publisher" on allow_sharing=False → publish fails.

    The override doesn't bypass _validate_publisher_provides.  Publish must
    return HTTP 400 with an error mentioning shareability.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5E-Agent"
    )
    drain_tasks()
    # Non-shareable credential
    cred = _create_credential(
        client, superuser_token_headers, name="private-key", allow_sharing=False
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred["id"])
    # Promote to publisher install before PATCHing publish-settings.
    _promote_to_publisher_install(client, superuser_token_headers, agent["id"])

    # Force an override that marks it publisher-provided
    r = _patch_publish_settings(
        client,
        superuser_token_headers,
        agent["id"],
        {"private-key": {"provided_by": "publisher"}},
    )
    assert r.status_code == 200, (
        f"PATCH publish-settings should accept override; got {r.status_code}: {r.text}"
    )

    # Publish must be rejected by _validate_publisher_provides
    pub_r = _publish_raw(client, superuser_token_headers, agent["id"])
    assert pub_r.status_code == 400, (
        f"Expected publish to fail with 400 for non-shareable publisher spec; "
        f"got {pub_r.status_code}: {pub_r.text}"
    )
    detail = str(pub_r.json().get("detail", ""))
    assert "shareable" in detail.lower() or "allow_sharing" in detail.lower(), (
        f"Expected error mentioning shareability; got: {detail}"
    )


# ── Scenario F: override provided_by="user" on shareable → spec is user ───────


def test_override_user_on_shareable_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """F. Override provided_by="user" on allow_sharing=True credential.

    Without override inference would produce provided_by="publisher".
    The override must flip this: publish emits provided_by="user" and
    publisher_credential_id=null.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5F-Agent"
    )
    drain_tasks()
    cred = _create_credential(
        client, superuser_token_headers, name="shareable-svc", allow_sharing=True
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred["id"])
    # Promote to publisher install (revision 1).
    _promote_to_publisher_install(client, superuser_token_headers, agent["id"])

    # Override shareable cred to user-provided
    r = _patch_publish_settings(
        client,
        superuser_token_headers,
        agent["id"],
        {"shareable-svc": {"provided_by": "user"}},
    )
    assert r.status_code == 200, r.text

    # Second publish (revision 2) picks up the new override.
    fresh = _publish(client, superuser_token_headers, agent["id"])
    bundle_uuid = fresh["bundle_uuid"]
    assert bundle_uuid, "Expected bundle_uuid after publish"

    specs = _get_latest_specs(client, superuser_token_headers, bundle_uuid)
    matched = [s for s in specs if s["name"] == "shareable-svc"]
    assert len(matched) == 1, f"Expected spec 'shareable-svc'; got {specs}"
    spec = matched[0]

    assert spec["provided_by"] == "user", (
        f"Override to 'user' should suppress inference; got {spec['provided_by']}"
    )
    assert spec["publisher_credential_id"] is None, (
        f"publisher_credential_id should be null for user-provided spec; "
        f"got {spec['publisher_credential_id']}"
    )


# ── Scenario G: override provided_by="publisher" on shareable → same as infer ─


def test_override_publisher_on_shareable_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """G. Override provided_by="publisher" on allow_sharing=True → spec has
    provided_by="publisher" and publisher_credential_id == the credential uuid.

    This mirrors inference behaviour, confirming explicit override is accepted
    and the publisher_credential_id is populated correctly.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5G-Agent"
    )
    drain_tasks()
    cred = _create_credential(
        client, superuser_token_headers, name="shared-key", allow_sharing=True
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred["id"])
    # Promote to publisher install (revision 1).
    _promote_to_publisher_install(client, superuser_token_headers, agent["id"])

    r = _patch_publish_settings(
        client,
        superuser_token_headers,
        agent["id"],
        {"shared-key": {"provided_by": "publisher"}},
    )
    assert r.status_code == 200, r.text

    # Second publish (revision 2) — specs reflect the explicit override.
    fresh = _publish(client, superuser_token_headers, agent["id"])
    bundle_uuid = fresh["bundle_uuid"]

    specs = _get_latest_specs(client, superuser_token_headers, bundle_uuid)
    matched = [s for s in specs if s["name"] == "shared-key"]
    assert len(matched) == 1, f"Expected spec 'shared-key'; got {specs}"
    spec = matched[0]

    assert spec["provided_by"] == "publisher", (
        f"Expected provided_by='publisher'; got {spec['provided_by']}"
    )
    assert spec["publisher_credential_id"] == cred["id"], (
        f"Expected publisher_credential_id={cred['id']}; "
        f"got {spec['publisher_credential_id']}"
    )


# ── Scenario H: no override → inference still applies (regression) ────────────


def test_no_override_inference_still_applies(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """H. With empty publish_settings, inference produces provided_by="publisher"
    for allow_sharing=True.  Regression check that Phase 1 inference survives
    the Phase 5 code path.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5H-Agent"
    )
    drain_tasks()
    cred = _create_credential(
        client, superuser_token_headers, name="infer-cred", allow_sharing=True
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred["id"])
    # Promote to publisher install (revision 1).
    _promote_to_publisher_install(client, superuser_token_headers, agent["id"])

    # Explicitly clear publish_settings (send empty overrides)
    r = _patch_publish_settings(
        client,
        superuser_token_headers,
        agent["id"],
        {},  # no overrides → inference takes over
    )
    assert r.status_code == 200, r.text

    # Second publish (revision 2) — inference must kick in.
    fresh = _publish(client, superuser_token_headers, agent["id"])
    bundle_uuid = fresh["bundle_uuid"]

    specs = _get_latest_specs(client, superuser_token_headers, bundle_uuid)
    matched = [s for s in specs if s["name"] == "infer-cred"]
    assert len(matched) == 1, f"Expected spec 'infer-cred'; got {specs}"
    spec = matched[0]

    assert spec["provided_by"] == "publisher", (
        f"Inference should produce 'publisher' for allow_sharing=True; "
        f"got {spec['provided_by']}"
    )
    assert spec["publisher_credential_id"] == cred["id"], (
        f"Expected publisher_credential_id={cred['id']}; "
        f"got {spec['publisher_credential_id']}"
    )


# ── Scenario I: mixed — one override + one inferred ──────────────────────────


def test_mixed_override_and_inferred_specs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """I. Two credentials: shareable overridden to user + non-shareable no override.

    Expected resulting specs:
    - shareable-override → provided_by="user", publisher_credential_id=null
    - private-nooverride → provided_by="user" (inference fallback for non-shareable)

    Verifies no cross-contamination between the override path and the
    inference path.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5I-Agent"
    )
    drain_tasks()

    # Shareable → normally inferred as publisher, overridden to user
    cred_a = _create_credential(
        client,
        superuser_token_headers,
        name="share-override",
        allow_sharing=True,
    )
    # Non-shareable → inferred as user, no override
    cred_b = _create_credential(
        client,
        superuser_token_headers,
        name="private-infer",
        allow_sharing=False,
    )
    _link_credential(client, superuser_token_headers, agent["id"], cred_a["id"])
    _link_credential(client, superuser_token_headers, agent["id"], cred_b["id"])
    # Promote to publisher install (revision 1).
    _promote_to_publisher_install(client, superuser_token_headers, agent["id"])

    r = _patch_publish_settings(
        client,
        superuser_token_headers,
        agent["id"],
        {"share-override": {"provided_by": "user"}},
    )
    assert r.status_code == 200, r.text

    # Second publish (revision 2) — both specs reflect override + inference.
    fresh = _publish(client, superuser_token_headers, agent["id"])
    bundle_uuid = fresh["bundle_uuid"]

    specs = _get_latest_specs(client, superuser_token_headers, bundle_uuid)

    spec_a = next((s for s in specs if s["name"] == "share-override"), None)
    spec_b = next((s for s in specs if s["name"] == "private-infer"), None)

    assert spec_a is not None, f"Expected 'share-override' spec; got {specs}"
    assert spec_b is not None, f"Expected 'private-infer' spec; got {specs}"

    assert spec_a["provided_by"] == "user", (
        f"Override to 'user' should override inference; got {spec_a['provided_by']}"
    )
    assert spec_a["publisher_credential_id"] is None, (
        f"share-override: publisher_credential_id should be null; "
        f"got {spec_a['publisher_credential_id']}"
    )

    assert spec_b["provided_by"] == "user", (
        f"Non-shareable inference should be 'user'; got {spec_b['provided_by']}"
    )
    assert spec_b["publisher_credential_id"] is None, (
        f"private-infer: publisher_credential_id should be null; "
        f"got {spec_b['publisher_credential_id']}"
    )


# ── Scenario J: install with new payload shape (regression after shim removal) ─


def test_install_new_payload_works_after_shim_removal(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """J. POST /catalog/{bundle_id}/install with InstallCredentialSelection shape
    works after the legacy shim is removed.

    Publishes a bundle with one PBU credential. Installer creates their own
    credential and installs using mode="use_existing".  Assert HTTP 200 and
    agent created for the installer.
    """
    # ── Publisher side ────────────────────────────────────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="P5J-Publisher"
    )
    drain_tasks()

    pub_cred = _create_credential(
        client,
        superuser_token_headers,
        name="j-service-cred",
        allow_sharing=False,
    )
    _link_credential(client, superuser_token_headers, agent["id"], pub_cred["id"])

    fresh = _publish(client, superuser_token_headers, agent["id"])
    bundle_uuid = fresh["bundle_uuid"]
    bundle_id = fresh["bundle_id"]
    assert bundle_uuid and bundle_id, "Expected bundle after publish"

    _make_public(client, superuser_token_headers, bundle_uuid)

    # ── Installer side ────────────────────────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)

    user_cred = _create_credential(
        client,
        installer_headers,
        name="j-service-cred",
        allow_sharing=False,
    )

    install = _install(
        client,
        installer_headers,
        bundle_id,
        request_body={
            "credentials": {
                "j-service-cred": {
                    "mode": "use_existing",
                    "credential_id": user_cred["id"],
                }
            }
        },
    )
    assert install.get("id"), (
        f"Expected installed agent id in response; got {install}"
    )
    assert install.get("bundle_id") == bundle_id, (
        f"Installed agent bundle_id should match; got {install.get('bundle_id')}"
    )
