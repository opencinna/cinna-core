"""
Tests for the Mail Server Configuration API (`/api/v1/mail-servers/`).

`MailServerConfig` became server-scoped and superuser-only in phase 4 of the
channels & identity unification refactor: the per-agent Email Integration
that used to be the only thing exercising this router was deleted outright
(see `docs/plans/channels_identity_unification/phase_4_transport_split_and_email.md`
§3). This file is this router's first direct coverage.

Covers:
  - Every route is superuser-only (403 for a non-superuser; success for the
    superuser).
  - CRUD round-trip; the password is write-only — never echoed back on
    create, get, list, or update.
  - `test-connection`, mocked for both a success and a failure path (real
    IMAP/SMTP hosts don't exist in the test environment).
  - The deletion guard: a mail server referenced by a `ServerChannel`
    `config` (`incoming_server_id` / `outgoing_server_id`) cannot be
    deleted (409, with the impact payload); an unreferenced one can be.
  - The deletion guard matches the id, not its spelling: a non-canonical
    (uppercase) UUID in a channel's `config` still blocks deletion.

No `email` channel adapter is registered yet at this commit (that lands
later in this phase), so the deletion-guard scenario references a mail
server id from a `google_chat` channel's `config` dict instead. The guard
itself (`MailServerService.get_deletion_impact`) is a generic scan over
every channel's `config` dict for those two keys, irrespective of
`channel_type`, so a `google_chat` channel exercises the real guard.
"""
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.mail_server import create_imap_server, create_smtp_server
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_BASE = f"{API}/mail-servers"
_CHANNELS_BASE = f"{API}/admin/server-channels"


# ── Local helpers ────────────────────────────────────────────────────────


def _assert_no_secret_leak(body: dict) -> None:
    """The password must never be echoed back, under any key."""
    assert "password" not in body
    assert "encrypted_password" not in body
    assert body.get("has_password") is True


def _create_channel_referencing_server(
    client: TestClient,
    headers: dict[str, str],
    *,
    role_key: str,
    server_id: str,
) -> dict:
    """Create a `google_chat` channel whose config carries a mail-server id.

    The deletion guard scans every channel's `config` dict for
    `incoming_server_id` / `outgoing_server_id` regardless of `channel_type`
    (`MailServerService.get_deletion_impact`) — an already-registered
    channel type is enough to exercise it; nothing about the guard is
    Google-Chat-specific. `google_chat`'s own `validate_config` only checks
    for a numeric `project_number` and ignores unrelated extra keys.
    """
    payload: dict[str, Any] = {
        "channel_type": "google_chat",
        "name": f"channel-{random_lower_string()[:8]}",
        "enabled": True,
        "auto_register_users": False,
        "config": {"project_number": "123456789012", role_key: server_id},
    }
    r = client.post(_CHANNELS_BASE, headers=headers, json=payload)
    assert r.status_code == 200, f"Create channel failed: {r.text}"
    return r.json()


def _delete_channel(client: TestClient, headers: dict[str, str], channel_id: str) -> None:
    r = client.delete(f"{_CHANNELS_BASE}/{channel_id}", headers=headers)
    assert r.status_code == 204, r.text


# ── Tests ────────────────────────────────────────────────────────────────


def test_mail_server_crud_lifecycle_and_superuser_guard(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Full mail-server CRUD lifecycle, gated superuser-only end to end:

      1. A non-superuser is rejected (403) on list and create.
      2. The superuser creates an IMAP and an SMTP server; the password is
         never echoed back on create.
      3. A non-superuser is rejected (403) on get, update, delete, and
         test-connection against those ids.
      4. The superuser's GET and list (with and without a `server_type`
         filter) round-trip the same non-secret fields — still no password.
      5. Update changes the host/port/username and rotates the password;
         the response reflects the new fields and still never echoes one.
      6. `test-connection` is exercised for both a mocked success and a
         mocked failure (IMAP), and a mocked success (SMTP).
      7. Delete succeeds for a server nothing references, and a subsequent
         GET 404s; a non-existent id also 404s for the superuser.
    """
    _, other_headers = create_random_user_with_headers(client)

    imap_payload = {
        "name": "Support IMAP",
        "server_type": "imap",
        "host": "imap.test.com",
        "port": 993,
        "encryption_type": "ssl",
        "username": "support@test.com",
        "password": "s3cret-imap-pw",
    }
    smtp_payload = {
        "name": "Support SMTP",
        "server_type": "smtp",
        "host": "smtp.test.com",
        "port": 465,
        "encryption_type": "ssl",
        "username": "support@test.com",
        "password": "s3cret-smtp-pw",
    }

    # ── Phase 1: non-superuser rejected on list/create ────────────────────
    assert client.get(f"{_BASE}/", headers=other_headers).status_code == 403
    assert (
        client.post(f"{_BASE}/", headers=other_headers, json=imap_payload).status_code
        == 403
    )

    # ── Phase 2: superuser creates IMAP + SMTP servers ────────────────────
    imap = create_imap_server(
        client,
        superuser_token_headers,
        host=imap_payload["host"],
        port=imap_payload["port"],
        username=imap_payload["username"],
        password=imap_payload["password"],
        name=imap_payload["name"],
    )
    smtp = create_smtp_server(
        client,
        superuser_token_headers,
        host=smtp_payload["host"],
        port=smtp_payload["port"],
        username=smtp_payload["username"],
        password=smtp_payload["password"],
        name=smtp_payload["name"],
    )
    imap_id, smtp_id = imap["id"], smtp["id"]

    for created, payload in ((imap, imap_payload), (smtp, smtp_payload)):
        assert created["name"] == payload["name"]
        assert created["server_type"] == payload["server_type"]
        assert created["host"] == payload["host"]
        assert created["port"] == payload["port"]
        assert created["username"] == payload["username"]
        _assert_no_secret_leak(created)

    # ── Phase 3: non-superuser rejected on get/update/delete/test-connection
    for method, url, kwargs in (
        ("get", f"{_BASE}/{imap_id}", {}),
        ("put", f"{_BASE}/{imap_id}", {"json": {"host": "evil.example.com"}}),
        ("delete", f"{_BASE}/{imap_id}", {}),
        ("post", f"{_BASE}/{imap_id}/test-connection", {}),
    ):
        r = getattr(client, method)(url, headers=other_headers, **kwargs)
        assert r.status_code == 403, (
            f"{method.upper()} {url} expected 403, got {r.status_code}: {r.text}"
        )

    # ── Phase 4: superuser GET + list round-trip, no secret leak ──────────
    r = client.get(f"{_BASE}/{imap_id}", headers=superuser_token_headers)
    assert r.status_code == 200
    fetched = r.json()
    assert fetched["id"] == imap_id
    _assert_no_secret_leak(fetched)

    r = client.get(f"{_BASE}/", headers=superuser_token_headers)
    assert r.status_code == 200
    body = r.json()
    ids_in_list = {s["id"] for s in body["data"]}
    assert imap_id in ids_in_list and smtp_id in ids_in_list
    for s in body["data"]:
        assert "password" not in s and "encrypted_password" not in s

    r = client.get(
        f"{_BASE}/", headers=superuser_token_headers, params={"server_type": "imap"}
    )
    assert r.status_code == 200
    imap_only = r.json()
    imap_only_ids = {s["id"] for s in imap_only["data"]}
    assert imap_id in imap_only_ids
    assert smtp_id not in imap_only_ids
    assert all(s["server_type"] == "imap" for s in imap_only["data"])

    # ── Phase 5: superuser updates host/port/username + rotates password ──
    r = client.put(
        f"{_BASE}/{imap_id}",
        headers=superuser_token_headers,
        json={
            "host": "imap2.test.com",
            "port": 994,
            "username": "support2@test.com",
            "password": "rotated-password",
        },
    )
    assert r.status_code == 200
    updated = r.json()
    assert updated["host"] == "imap2.test.com"
    assert updated["port"] == 994
    assert updated["username"] == "support2@test.com"
    _assert_no_secret_leak(updated)

    # ── Phase 6: test-connection — mocked success then mocked failure ─────
    fake_imap_conn = MagicMock()
    with patch(
        "app.services.email.mail_server_service.imaplib.IMAP4_SSL",
        return_value=fake_imap_conn,
    ):
        r = client.post(
            f"{_BASE}/{imap_id}/test-connection", headers=superuser_token_headers
        )
    assert r.status_code == 200
    assert "successful" in r.json()["message"].lower()
    fake_imap_conn.login.assert_called_once()

    failing_imap_conn = MagicMock()
    failing_imap_conn.login.side_effect = Exception("boom")
    with patch(
        "app.services.email.mail_server_service.imaplib.IMAP4_SSL",
        return_value=failing_imap_conn,
    ):
        r = client.post(
            f"{_BASE}/{imap_id}/test-connection", headers=superuser_token_headers
        )
    assert r.status_code == 400
    assert "IMAP connection failed" in r.json()["detail"]

    fake_smtp_conn = MagicMock()
    with patch(
        "app.services.email.mail_server_service.smtplib.SMTP_SSL",
        return_value=fake_smtp_conn,
    ):
        r = client.post(
            f"{_BASE}/{smtp_id}/test-connection", headers=superuser_token_headers
        )
    assert r.status_code == 200
    assert "successful" in r.json()["message"].lower()
    fake_smtp_conn.login.assert_called_once()

    # ── Phase 7: delete succeeds when nothing references the server ───────
    r = client.delete(f"{_BASE}/{smtp_id}", headers=superuser_token_headers)
    assert r.status_code == 200
    assert r.json()["message"] == "Mail server deleted successfully"

    r = client.get(f"{_BASE}/{smtp_id}", headers=superuser_token_headers)
    assert r.status_code == 404

    ghost = str(uuid.uuid4())
    assert client.get(f"{_BASE}/{ghost}", headers=superuser_token_headers).status_code == 404


def test_mail_server_deletion_blocked_while_referenced_by_channel(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A mail server referenced by a channel's `config` cannot be deleted until
    the reference is gone — in both directions:

      1. `incoming_server_id` (an IMAP server) blocks deletion (409) with an
         impact payload naming the referencing channel and role "incoming".
      2. `outgoing_server_id` (an SMTP server) blocks deletion (409) the
         same way, role "outgoing".
      3. Deleting the referencing channel lifts the guard in both cases —
         the guard does not block deletion unconditionally.
    """
    imap = create_imap_server(client, superuser_token_headers)
    smtp = create_smtp_server(client, superuser_token_headers)
    imap_id, smtp_id = imap["id"], smtp["id"]

    # ── incoming_server_id direction ───────────────────────────────────────
    incoming_channel = _create_channel_referencing_server(
        client, superuser_token_headers, role_key="incoming_server_id", server_id=imap_id
    )

    r = client.delete(f"{_BASE}/{imap_id}", headers=superuser_token_headers)
    assert r.status_code == 409
    impact = r.json()["detail"]
    usages = impact["channel_usages"]
    assert len(usages) == 1
    assert usages[0]["channel_id"] == incoming_channel["id"]
    assert usages[0]["channel_name"] == incoming_channel["name"]
    assert usages[0]["role"] == "incoming"

    # ── outgoing_server_id direction ────────────────────────────────────────
    outgoing_channel = _create_channel_referencing_server(
        client, superuser_token_headers, role_key="outgoing_server_id", server_id=smtp_id
    )

    r = client.delete(f"{_BASE}/{smtp_id}", headers=superuser_token_headers)
    assert r.status_code == 409
    impact = r.json()["detail"]
    usages = impact["channel_usages"]
    assert len(usages) == 1
    assert usages[0]["channel_id"] == outgoing_channel["id"]
    assert usages[0]["channel_name"] == outgoing_channel["name"]
    assert usages[0]["role"] == "outgoing"

    # ── Removing the reference lifts the guard ─────────────────────────────
    _delete_channel(client, superuser_token_headers, incoming_channel["id"])
    r = client.delete(f"{_BASE}/{imap_id}", headers=superuser_token_headers)
    assert r.status_code == 200

    _delete_channel(client, superuser_token_headers, outgoing_channel["id"])
    r = client.delete(f"{_BASE}/{smtp_id}", headers=superuser_token_headers)
    assert r.status_code == 200


def test_mail_server_deletion_guard_matches_non_canonical_uuid_spelling(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The guard must match the id, not its spelling.

    `ServerChannel.config` is free-form JSON that no adapter validates for
    `incoming_server_id` / `outgoing_server_id`, so the stored value can be
    any legal spelling of a UUID — uppercase hex here, but equally
    `{braces}`, `urn:uuid:`, or hyphenless. A string comparison against
    `str(server_id)` (always canonical lowercase-hyphenated) sees every one of
    those as a different server, reports an empty impact, and lets the delete
    through — leaving a live channel pointing at an id that resolves to
    nothing, which is exactly what this guard exists to prevent. The happy
    path hides it: an id copied out of an API response is always canonical.

    So: store the id UPPERCASED and assert the delete is still refused with
    409, naming the referencing channel.
    """
    imap = create_imap_server(client, superuser_token_headers)
    imap_id = imap["id"]

    channel = _create_channel_referencing_server(
        client,
        superuser_token_headers,
        role_key="incoming_server_id",
        server_id=imap_id.upper(),
    )
    # Guard the guard: this must genuinely be a different string, or the test
    # would pass against the string comparison it is written to catch.
    assert imap_id.upper() != imap_id

    r = client.delete(f"{_BASE}/{imap_id}", headers=superuser_token_headers)
    assert r.status_code == 409, (
        "Uppercase mail-server id in channel config did not block deletion — "
        f"got {r.status_code}: {r.text}"
    )
    usages = r.json()["detail"]["channel_usages"]
    assert len(usages) == 1
    assert usages[0]["channel_id"] == channel["id"]
    assert usages[0]["role"] == "incoming"

    # And the guard still lifts once the reference is gone.
    _delete_channel(client, superuser_token_headers, channel["id"])
    assert (
        client.delete(f"{_BASE}/{imap_id}", headers=superuser_token_headers).status_code
        == 200
    )
