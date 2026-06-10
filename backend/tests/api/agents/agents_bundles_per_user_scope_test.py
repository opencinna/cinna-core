"""Phase 6 tests — Groups 3 & 4: per-user scoped end-to-end + sharing ordering.

Group 3 — Per-user scoped end-to-end
-------------------------------------
Two installers; two DIFFERENT pre-shared api_token second tokens with the SAME
``service_uri``.

  3a. Each install's auto-detect (``build_install_context``) suggests the
      correct per-user token (installer A → token A, installer B → token B).
  3b. After install with ``mode="use_existing"`` on the suggested id, each
      install's readiness gate is ``ready``.
  3c. An installer with NO pre-shared token lands in ``needs_setup``
      (placeholder), confirming security invariant I3.

Group 4 — Credential sharing via UI ordering
---------------------------------------------
  4a. Share-before-install: token shared first → install auto-links it via the
      ``service_uri`` matcher → readiness gate ``ready``.
  4b. Manual-link-after fallback: token shared AFTER install → install created a
      placeholder (``needs_setup``); after the user manually links the now-shared
      token via the Credentials tab path (``PUT /agents/{id}/setup-credentials``),
      the gate flips to ``ready``.

Credential sharing goes through the public ``POST /credentials/{id}/shares``
API; every shared token in this file is created with ``allow_sharing=True``.
The setup-credential PUT path is also tested through the API surface.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    get_install_context as _install_context,
    install_bundle as _install,
    link_bundle_credential_to_agent as _link_credential_to_agent,
    make_bundle_public as _make_public,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle as _publish,
)
from tests.utils.credential import share_credential_via_api

API = settings.API_V1_STR


# ── Module-level helpers ──────────────────────────────────────────────────────
# Shared bundle helpers (_make_user_and_headers, _publish, _make_public,
# _install, _install_context, _link_credential_to_agent) are imported above
# from tests.utils.bundle. Only the service_uri-aware credential factory is local.


def _create_api_token_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    allow_sharing: bool = False,
    service_uri: str | None = None,
) -> dict:
    body: dict = {
        "name": name,
        "type": "api_token",
        "allow_sharing": allow_sharing,
        "credential_data": {
            "api_token_type": "bearer",
            "api_token_template": "Authorization: Bearer {TOKEN}",
            "api_token": f"test-token-{uuid.uuid4().hex[:8]}",
        },
    }
    if service_uri is not None:
        body["service_uri"] = service_uri
    r = client.post(f"{API}/credentials/", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _get_setup_status(
    client: TestClient, headers: dict[str, str], install_id: str
) -> dict:
    r = client.get(f"{API}/agents/{install_id}/setup-status", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── Group 3 — Per-user scoped end-to-end ─────────────────────────────────────


def test_per_user_scoped_install_two_installers_correct_token_suggested(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """3a-3b. Two installers with distinct per-user tokens sharing the same service_uri.

    Each install's auto-detect suggests the correct per-user token; installing
    with mode="use_existing" on the suggested id makes the gate ready.

    Scenario:
      1. Publisher publishes a bundle with one api_token spec stamped
         service_uri=SLOT_URI.
      2. Publisher pre-creates two per-user tokens (token A, token B)
         both stamped with SLOT_URI, shared respectively to installer A and B.
      3. Each installer's install-context suggests their own token (not the other's).
      4. Each installer installs with mode="use_existing" + their suggested token id.
      5. Each install's readiness gate returns "ready".
    """
    slot_uri = f"slot://per-user-e2e-{uuid.uuid4().hex[:6]}"
    spec_name = f"per-user-spec-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publisher publishes the bundle ────────────────────────────────
    pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_sharing=False,
        service_uri=slot_uri,
    )
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"PerUser-3ab-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # ── Phase 2: Publisher pre-creates per-user tokens ────────────────────────
    token_a = _create_api_token_credential(
        client,
        superuser_token_headers,
        name="company-a-second-token",  # different name from spec
        allow_sharing=True,
        service_uri=slot_uri,
    )
    token_a_id = uuid.UUID(token_a["id"])

    token_b = _create_api_token_credential(
        client,
        superuser_token_headers,
        name="company-b-second-token",  # different name from spec
        allow_sharing=True,
        service_uri=slot_uri,
    )
    token_b_id = uuid.UUID(token_b["id"])

    # ── Phase 3: Set up the two installers ────────────────────────────────────
    installer_a, installer_a_headers = _make_user_and_headers(client)
    installer_b, installer_b_headers = _make_user_and_headers(client)

    # Share token A with installer A only; token B with installer B only
    share_credential_via_api(
        client, superuser_token_headers, token_a["id"], installer_a["email"]
    )
    share_credential_via_api(
        client, superuser_token_headers, token_b["id"], installer_b["email"]
    )

    # ── Phase 3: install-context suggests correct token per installer ──────────
    ctx_a = _install_context(client, installer_a_headers, bundle_id)
    ctx_b = _install_context(client, installer_b_headers, bundle_id)

    spec_a = ctx_a["service_specs"][0]
    spec_b = ctx_b["service_specs"][0]

    assert spec_a["suggested_credential_id"] == str(token_a_id), (
        f"Installer A must get token A suggestion; "
        f"expected {token_a_id}, got {spec_a['suggested_credential_id']}"
    )
    assert spec_b["suggested_credential_id"] == str(token_b_id), (
        f"Installer B must get token B suggestion; "
        f"expected {token_b_id}, got {spec_b['suggested_credential_id']}"
    )

    # ── Phase 4 + 5: Each installer installs with mode=use_existing → gate ready ─
    install_a = _install(
        client,
        installer_a_headers,
        bundle_id,
        request_body={
            "credentials": {
                spec_name: {
                    "mode": "use_existing",
                    "credential_id": str(token_a_id),
                }
            }
        },
    )
    status_a = _get_setup_status(client, installer_a_headers, install_a["id"])
    assert status_a["status"] == "ready", (
        f"Install A gate must be ready after linking token A via use_existing; "
        f"got {status_a['status']} missing={status_a.get('missing')}"
    )

    install_b = _install(
        client,
        installer_b_headers,
        bundle_id,
        request_body={
            "credentials": {
                spec_name: {
                    "mode": "use_existing",
                    "credential_id": str(token_b_id),
                }
            }
        },
    )
    status_b = _get_setup_status(client, installer_b_headers, install_b["id"])
    assert status_b["status"] == "ready", (
        f"Install B gate must be ready after linking token B via use_existing; "
        f"got {status_b['status']} missing={status_b.get('missing')}"
    )


def test_per_user_scoped_no_token_lands_in_needs_setup(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """3c. An installer with NO pre-shared token lands in needs_setup (I3).

    A third installer who has no pre-shared token for the service_uri slot:
      - install-context suggests None.
      - quick install creates a placeholder credential.
      - readiness gate returns needs_setup.
    Revoking access = no token → no access. Confirms security invariant I3.
    """
    slot_uri = f"slot://no-token-i3-{uuid.uuid4().hex[:6]}"
    spec_name = f"no-token-spec-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publish bundle with service_uri spec ─────────────────────────
    pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_sharing=False,
        service_uri=slot_uri,
    )
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"PerUser-3c-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # ── Phase 2: Installer with NO pre-shared token ───────────────────────────
    no_token_installer, no_token_headers = _make_user_and_headers(client)

    # ── Phase 3: install-context → no suggestion ──────────────────────────────
    ctx = _install_context(client, no_token_headers, bundle_id)
    spec = ctx["service_specs"][0]
    assert spec["suggested_credential_id"] is None, (
        "No pre-shared token → suggested_credential_id must be None (I3). "
        f"Got {spec['suggested_credential_id']}"
    )

    # ── Phase 4: Quick install → placeholder created ──────────────────────────
    install = _install(client, no_token_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 5: Gate → needs_setup (I3 confirmed) ────────────────────────────
    status = _get_setup_status(client, no_token_headers, install_id)
    assert status["status"] == "needs_setup", (
        f"No pre-shared token → gate must be needs_setup (I3); "
        f"got {status['status']}"
    )
    assert isinstance(status.get("missing"), list) and len(status["missing"]) >= 1, (
        f"needs_setup must include at least one missing item; got {status.get('missing')}"
    )
    # The missing reason must be placeholder-related (placeholder_empty)
    reasons = [m["reason"] for m in status["missing"]]
    assert any("placeholder" in r for r in reasons), (
        f"Missing reason must mention placeholder; got {reasons}"
    )


# ── Group 4 — Credential sharing ordering ────────────────────────────────────


def test_share_before_install_auto_links_via_service_uri_gate_ready(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """4a. Share-before-install: token shared first → install auto-links it → gate ready.

    The "happy path" ordering constraint: publisher pre-shares the per-user
    token BEFORE the user installs. The install-context suggests the token
    (service_uri match); the user installs with mode=use_existing; gate=ready.

    This is the expected recommended flow from the plan.
    """
    slot_uri = f"slot://share-before-{uuid.uuid4().hex[:6]}"
    spec_name = f"sbi-spec-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publish bundle ────────────────────────────────────────────────
    pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_sharing=False,
        service_uri=slot_uri,
    )
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"Ordering-4a-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # ── Phase 2: Publisher creates + shares the per-user token BEFORE install ──
    user_token = _create_api_token_credential(
        client,
        superuser_token_headers,
        name="user-pre-shared-token",   # different name from spec
        allow_sharing=True,
        service_uri=slot_uri,           # matching service_uri
    )
    user_token_id = uuid.UUID(user_token["id"])

    installer, installer_headers = _make_user_and_headers(client)

    # Share BEFORE install
    share_credential_via_api(
        client, superuser_token_headers, user_token["id"], installer["email"]
    )

    # ── Phase 3: GET install-context → suggestion = the pre-shared token ──────
    ctx = _install_context(client, installer_headers, bundle_id)
    spec = ctx["service_specs"][0]
    assert spec["suggested_credential_id"] == str(user_token_id), (
        f"Share-before-install: suggested_credential_id must be the pre-shared "
        f"token {user_token_id}; got {spec['suggested_credential_id']}"
    )

    # ── Phase 4: Install with mode=use_existing on the suggested token ─────────
    install = _install(
        client,
        installer_headers,
        bundle_id,
        request_body={
            "credentials": {
                spec_name: {
                    "mode": "use_existing",
                    "credential_id": str(user_token_id),
                }
            }
        },
    )
    install_id = install["id"]

    # ── Phase 5: Gate → ready ─────────────────────────────────────────────────
    status = _get_setup_status(client, installer_headers, install_id)
    assert status["status"] == "ready", (
        f"Share-before-install: gate must be ready after auto-linking pre-shared token; "
        f"got {status['status']} missing={status.get('missing')}"
    )


def test_share_after_install_placeholder_then_manual_link_flips_gate_ready(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """4b. Manual-link-after fallback: token shared after install → needs_setup →
    user fills placeholder via setup-credentials PUT → gate ready.

    The fallback ordering path: token shared AFTER the user has already
    installed. At install time, no matching credential exists → placeholder
    created → needs_setup. Publisher then shares the token. The user manually
    fills the placeholder via PUT /agents/{id}/setup-credentials/{cred_id}
    (the Credentials tab path). The gate flips to ready.

    This test verifies OQ3's resolution: no auto-rematch flow in MVP;
    the user's manual PUT path is the accepted fallback.
    """
    slot_uri = f"slot://share-after-{uuid.uuid4().hex[:6]}"
    spec_name = f"sai-spec-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publish bundle ────────────────────────────────────────────────
    pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_sharing=False,
        service_uri=slot_uri,
    )
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"Ordering-4b-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # ── Phase 2: Installer installs WITHOUT the pre-shared token ──────────────
    installer, installer_headers = _make_user_and_headers(client)

    # No token shared yet → install-context has no suggestion
    ctx_before = _install_context(client, installer_headers, bundle_id)
    assert ctx_before["service_specs"][0]["suggested_credential_id"] is None, (
        "No token shared yet → suggestion must be None before install"
    )

    # Quick install → creates a placeholder
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    # Gate → needs_setup (placeholder was created)
    status_before = _get_setup_status(client, installer_headers, install_id)
    assert status_before["status"] == "needs_setup", (
        f"Token shared after install → gate must be needs_setup; "
        f"got {status_before['status']}"
    )

    # ── Phase 3: Publisher now shares the per-user token (AFTER install) ──────
    user_token = _create_api_token_credential(
        client,
        superuser_token_headers,
        name="late-shared-token",      # different name from spec
        allow_sharing=True,
        service_uri=slot_uri,
    )

    # Share after the install already happened
    share_credential_via_api(
        client, superuser_token_headers, user_token["id"], installer["email"]
    )

    # The install-context now would suggest the token (for informational purposes),
    # but the install already has a placeholder — no auto-rematch (OQ3 doc-not-build).
    # The user must manually fill the placeholder via the Credentials tab path.

    # ── Phase 4: Find the placeholder credential linked to the install ─────────
    creds = client.get(
        f"{API}/agents/{install_id}/credentials", headers=installer_headers
    )
    assert creds.status_code == 200, creds.text
    placeholders = [
        c for c in creds.json()["data"]
        if c["is_placeholder"] and c["owner_id"] == installer["id"]
    ]
    assert placeholders, (
        "Expected a placeholder Credential linked to the install after quick-install "
        "with no pre-shared token"
    )
    placeholder_id = placeholders[0]["id"]

    # ── Phase 5: User manually fills the placeholder via PUT setup-credentials ─
    r = client.put(
        f"{API}/agents/{install_id}/setup-credentials/{placeholder_id}",
        headers=installer_headers,
        json={
            "credential_data": {
                "api_token_type": "bearer",
                "api_token_template": "Authorization: Bearer {TOKEN}",
                "api_token": "real-user-token-value",
            }
        },
    )
    assert r.status_code == 200, (
        f"PUT setup-credentials must return 200; got {r.status_code}: {r.text}"
    )
    put_resp = r.json()
    assert put_resp["id"] == placeholder_id, (
        f"PUT response id must match the placeholder id; "
        f"got {put_resp['id']} != {placeholder_id}"
    )

    # ── Phase 6: Gate → ready after the manual fill ───────────────────────────
    status_after = _get_setup_status(client, installer_headers, install_id)
    assert status_after["status"] == "ready", (
        f"Gate must flip to ready after manual credential fill via PUT "
        f"setup-credentials; got {status_after['status']} "
        f"missing={status_after.get('missing')}"
    )
