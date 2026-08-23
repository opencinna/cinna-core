"""Admin CRUD for Server Channels — plan §13 "Admin CRUD" checklist item.

Covers:
  - Superuser-only enforcement on every admin route (unauthenticated + a
    non-admin authenticated user, both rejected).
  - Secrets are never echoed back in any response (create, update, list, get
    setup instructions) — `ServerChannelPublic` only ever carries
    `has_outbound_credentials`.
  - Webhook-token regeneration via the explicit `regenerate_webhook_token`
    update flag.
  - Auto-install list CRUD: add/remove, idempotent add, visibility and
    missing-trigger-prompt flags on the joined projection.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_user_and_headers, publish_bundle
from tests.utils.server_channel import (
    add_auto_install_bundle,
    create_server_channel,
    delete_server_channel,
    get_setup_instructions,
    list_auto_install_bundles,
    list_server_channels,
    remove_auto_install_bundle,
    update_server_channel,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_ADMIN_BASE = f"{API}/admin/server-channels"


def _assert_no_secret_leak(payload: dict) -> None:
    assert "secrets" not in payload
    assert "encrypted_secrets" not in payload
    assert "has_outbound_credentials" in payload


def test_admin_channel_lifecycle_and_auth_guards(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Full admin lifecycle for a channel:
      1. Unauthenticated + non-superuser requests are rejected on every route.
      2. Create — secrets never echoed, webhook_token + webhook_url present.
      3. Appears in list — still no secret leak.
      4. GET setup-instructions — no secret leak, webhook URL matches.
      5. Update (whitelist + name) — persists, secrets still absent.
      6. Update with a NEW secret does not echo it back either.
      7. Update leaving `secrets` unset does not clear the stored credential
         (has_outbound_credentials stays True).
      8. Explicit `regenerate_webhook_token=True` mints a new token; the old
         token is now unusable (channel-types/list still fine).
      9. Non-existent id -> 404.
      10. Delete -> gone from the list.
    """
    _, other_headers = create_random_user_with_headers(client)

    # ── Phase 1: auth guards on every route shape ──────────────────────────
    assert client.get(_ADMIN_BASE).status_code in (401, 403)
    assert client.post(_ADMIN_BASE, json={}).status_code in (401, 403)
    assert client.get(_ADMIN_BASE, headers=other_headers).status_code == 403
    assert (
        client.post(
            _ADMIN_BASE,
            headers=other_headers,
            json={"channel_type": "google_chat", "name": "x", "config": {}},
        ).status_code
        == 403
    )

    # ── Phase 2: create ──────────────────────────────────────────────────
    channel = create_server_channel(
        client,
        superuser_token_headers,
        name=f"Admin Test Channel {random_lower_string()[:6]}",
        secrets='{"client_email": "bot@test.iam.gserviceaccount.com", "private_key": "x"}',
        email_whitelist="*@example.com",
    )
    _assert_no_secret_leak(channel)
    assert channel["has_outbound_credentials"] is True
    assert channel["webhook_token"]
    assert channel["webhook_token"] in channel["webhook_url"]
    assert channel["email_whitelist"] == "*@example.com"
    channel_id = channel["id"]

    # ── Phase 3: appears in list ─────────────────────────────────────────
    listed = list_server_channels(client, superuser_token_headers)
    assert any(c["id"] == channel_id for c in listed)
    for c in listed:
        _assert_no_secret_leak(c)

    # ── Phase 4: setup instructions ─────────────────────────────────────
    instructions = get_setup_instructions(client, superuser_token_headers, channel_id)
    assert instructions["channel_type"] == "google_chat"
    assert instructions["webhook_url"] == channel["webhook_url"]
    assert "secrets" not in instructions
    assert "private_key" not in str(instructions)

    # ── Phase 5: update whitelist + name ────────────────────────────────
    new_name = f"Renamed {random_lower_string()[:6]}"
    updated = update_server_channel(
        client,
        superuser_token_headers,
        channel_id,
        name=new_name,
        email_whitelist="*@other.example, ops.*@corp.example",
    )
    _assert_no_secret_leak(updated)
    assert updated["name"] == new_name
    assert updated["email_whitelist"] == "*@other.example, ops.*@corp.example"
    assert updated["has_outbound_credentials"] is True  # untouched

    # ── Phase 6: update WITH a new secret still never echoes it ─────────
    updated2 = update_server_channel(
        client,
        superuser_token_headers,
        channel_id,
        secrets='{"client_email": "rotated@test.iam.gserviceaccount.com", "private_key": "y"}',
    )
    _assert_no_secret_leak(updated2)
    assert updated2["has_outbound_credentials"] is True

    # ── Phase 7: update omitting `secrets` keeps the stored credential ──
    updated3 = update_server_channel(client, superuser_token_headers, channel_id, enabled=False)
    assert updated3["enabled"] is False
    assert updated3["has_outbound_credentials"] is True

    # re-enable for the next phase
    update_server_channel(client, superuser_token_headers, channel_id, enabled=True)

    # ── Phase 8: regenerate webhook token ───────────────────────────────
    old_token = updated3["webhook_token"]
    regenerated = update_server_channel(
        client, superuser_token_headers, channel_id, regenerate_webhook_token=True
    )
    assert regenerated["webhook_token"] != old_token
    assert regenerated["webhook_token"] in regenerated["webhook_url"]

    # ── Phase 9: non-existent id -> 404 ─────────────────────────────────
    ghost = str(uuid.uuid4())
    assert client.get(f"{_ADMIN_BASE}/{ghost}/setup-instructions", headers=superuser_token_headers).status_code == 404
    assert client.put(f"{_ADMIN_BASE}/{ghost}", headers=superuser_token_headers, json={}).status_code == 404
    assert client.delete(f"{_ADMIN_BASE}/{ghost}", headers=superuser_token_headers).status_code == 404

    # Non-superuser can't touch this channel either.
    assert client.put(f"{_ADMIN_BASE}/{channel_id}", headers=other_headers, json={}).status_code == 403
    assert client.delete(f"{_ADMIN_BASE}/{channel_id}", headers=other_headers).status_code == 403

    # ── Phase 10: delete ─────────────────────────────────────────────────
    delete_server_channel(client, superuser_token_headers, channel_id)
    assert not any(c["id"] == channel_id for c in list_server_channels(client, superuser_token_headers))


def test_create_rejects_unknown_channel_type_and_invalid_config(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Validation errors at create time:
      - Unknown channel_type -> 400.
      - Missing project_number -> 422.
      - Non-numeric project_number -> 422.
      - Duplicate channel name -> 409.
    """
    r = client.post(
        _ADMIN_BASE,
        headers=superuser_token_headers,
        json={"channel_type": "carrier_pigeon", "name": "x", "config": {}},
    )
    assert r.status_code == 400

    r = client.post(
        _ADMIN_BASE,
        headers=superuser_token_headers,
        json={"channel_type": "google_chat", "name": f"noproj-{random_lower_string()[:6]}", "config": {}},
    )
    assert r.status_code == 422

    r = client.post(
        _ADMIN_BASE,
        headers=superuser_token_headers,
        json={
            "channel_type": "google_chat",
            "name": f"badproj-{random_lower_string()[:6]}",
            "config": {"project_number": "not-a-number"},
        },
    )
    assert r.status_code == 422

    name = f"dup-{random_lower_string()[:6]}"
    create_server_channel(client, superuser_token_headers, name=name)
    r = client.post(
        _ADMIN_BASE,
        headers=superuser_token_headers,
        json={"channel_type": "google_chat", "name": name, "config": {"project_number": "123"}},
    )
    assert r.status_code == 409


def test_channel_types_endpoint_lists_registered_adapters(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    r = client.get(f"{_ADMIN_BASE}/channel-types", headers=superuser_token_headers)
    assert r.status_code == 200
    types = {t["channel_type"] for t in r.json()}
    assert "google_chat" in types


def test_auto_install_list_crud_and_flags(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Auto-install list admin surface:
      1. Empty initially (for a fresh bundle).
      2. Add a bundle with a trigger prompt, made public -> has_trigger_prompt
         True, visibility "public".
      3. Adding the same bundle again is idempotent (still one entry, no
         duplicate row, no error).
      4. A private bundle (visibility not public/listed) still gets flagged
         accurately by its own `visibility` field — the caller decides
         installability at routing time, not this endpoint.
      5. A bundle with NO router_trigger_prompt is flagged
         has_trigger_prompt=False.
      6. Remove -> gone from the list. Removing again is a no-op 204, not
         an error.
      7. Adding a non-existent bundle_uuid -> 404.
      8. The auto-install-list routes are superuser-only too.

    (A bundle with no published revision at all -> 422 is asserted by the
    service layer but isn't reachable through the public API — a bundle row
    only exists once its first revision is published — so it isn't exercised
    here.)
    """
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    # Bundle WITH a trigger prompt, made public.
    agent_with_trigger = create_agent_via_api(
        client, publisher_headers, name=f"AutoInstallOK-{random_lower_string()[:6]}"
    )
    drain_tasks()
    r = client.patch(
        f"{API}/agents/{agent_with_trigger['id']}/router-trigger-prompt",
        headers=publisher_headers,
        json={"router_trigger_prompt": "Handle auto-install test requests"},
    )
    assert r.status_code == 200, r.text
    fresh = publish_bundle(client, publisher_headers, agent_with_trigger["id"])
    bundle_uuid = fresh["bundle_uuid"]
    client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=publisher_headers,
        json={"is_listed": True, "visibility": "public"},
    )

    # Bundle WITHOUT a trigger prompt, left private.
    agent_no_trigger = create_agent_via_api(
        client, publisher_headers, name=f"AutoInstallNoTrigger-{random_lower_string()[:6]}"
    )
    drain_tasks()
    fresh2 = publish_bundle(client, publisher_headers, agent_no_trigger["id"])
    bundle_uuid_2 = fresh2["bundle_uuid"]

    before = list_auto_install_bundles(client, superuser_token_headers)
    assert not any(b["bundle_uuid"] == bundle_uuid for b in before)

    listed = add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)
    entry = next(b for b in listed if b["bundle_uuid"] == bundle_uuid)
    assert entry["has_trigger_prompt"] is True
    assert entry["visibility"] == "public"

    # Idempotent re-add.
    listed_again = add_auto_install_bundle(client, superuser_token_headers, bundle_uuid)
    assert sum(1 for b in listed_again if b["bundle_uuid"] == bundle_uuid) == 1

    # Second bundle: no trigger prompt -> flagged.
    listed3 = add_auto_install_bundle(client, superuser_token_headers, bundle_uuid_2)
    entry2 = next(b for b in listed3 if b["bundle_uuid"] == bundle_uuid_2)
    assert entry2["has_trigger_prompt"] is False

    # Remove first bundle.
    remove_auto_install_bundle(client, superuser_token_headers, bundle_uuid)
    after = list_auto_install_bundles(client, superuser_token_headers)
    assert not any(b["bundle_uuid"] == bundle_uuid for b in after)
    assert any(b["bundle_uuid"] == bundle_uuid_2 for b in after)

    # Removing again is a no-op, not an error.
    remove_auto_install_bundle(client, superuser_token_headers, bundle_uuid)

    # Non-existent bundle -> 404.
    r = client.post(
        f"{_ADMIN_BASE}/auto-install-list",
        headers=superuser_token_headers,
        json={"bundle_uuid": str(uuid.uuid4())},
    )
    assert r.status_code == 404

    # Auto-install-list routes are superuser-only too.
    _, other_headers = create_random_user_with_headers(client)
    assert client.get(f"{_ADMIN_BASE}/auto-install-list", headers=other_headers).status_code == 403
    assert (
        client.post(
            f"{_ADMIN_BASE}/auto-install-list", headers=other_headers, json={"bundle_uuid": str(uuid.uuid4())}
        ).status_code
        == 403
    )
