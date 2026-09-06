"""Per-user key minting — membership, the mint, and every way it can go wrong.

THE INVARIANT EVERY TEST HERE IS PROTECTING
-------------------------------------------
**Every ``AICredential`` row that exists is usable.** There is no placeholder
credential with an empty key and no "preparing" state on the credential table.
The pre-key state lives on the membership row, which is why a person can be a
member of a minted record and hold nothing yet — and why the discovery cron, the
desktop account-config bundle and the environment credential bag needed no new
filters and no new concept.

The second invariant is that a **failure is durable**: bounded retries, then a
terminal ``failed`` that is never tidied away. "Account creation never fails
because provisioning failed" is only an honest promise if the failure is still
visible afterwards to somebody who can fix it.

Provider I/O is replaced through the **registry override**, never by patching a
module attribute, and the stub is the real provisioner with only its HTTP
swapped — so the spend-cap refusal, the null-secret rejection and the external-ref
shape are all exercised for real. See ``tests/stubs/key_provisioner_stub.py``.
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.utils import restore_session
from tests.utils.account_provisioning import (
    CHILD_CREDENTIAL_INSERT,
    failing_sql_statement,
)
from tests.utils.ai_provider import stub_minting_providers
from tests.utils.background_tasks import drain_tasks
from tests.utils.desktop_auth import obtain_desktop_tokens
from tests.utils.fixtures import (
    BACKGROUND_TASK_TARGETS_FULL,
    dropped_background_tasks,
    patched_background_tasks,
    patched_create_sessions,
)
from tests.utils.key_provisioning import (
    converge_keys,
    make_membership_due,
    mark_membership_minting,
)
from tests.utils.query_counter import count_queries
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_email, random_lower_string

# No agents or environments here; the heavy stubs would only slow it down. The
# two fixtures this module *does* need are declared below, because the pieces of
# provisioning that happen off the request path — the revoke — are exactly what
# several of these tests are about.
NEEDS_AGENT_STUBS = False
NEEDS_DEFAULT_CREDENTIALS = False

API = settings.API_V1_STR
ADMIN_CRED_BASE = f"{API}/admin/provider-admin-credentials"
MANAGED_BASE = f"{API}/admin/llm-providers"


@pytest.fixture(autouse=True)
def background_and_sessions(db: Session):
    """Collect fire-and-forget revokes instead of scheduling them.

    Without this the revoke would be a real task on the app's loop, running after
    the registry override has been torn down — i.e. against the real provider.
    Collecting it also makes it *assertable*: a test that never drains proves the
    revoke was scheduled and not that it worked, and both facts matter.
    """
    with patched_create_sessions(db), patched_background_tasks(
        BACKGROUND_TASK_TARGETS_FULL
    ):
        yield


# ── Helpers ─────────────────────────────────────────────────────────────────


def _admin_credential(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        f"{ADMIN_CRED_BASE}/",
        headers=headers,
        json={
            "name": f"Org {random_lower_string()[:8]}",
            "provider_type": "openai",
            "secret": "sk-admin-not-a-real-secret",
            "config": {"project_id": "proj_test", "spend_limit_cents": 5000},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _minted_parent(
    client: TestClient,
    headers: dict[str, str],
    *,
    admin_credential_id: str,
    target_user_ids: list[str] | None = None,
    auto_provision_roles: list[str] | None = None,
    set_as_default: bool = False,
    expected_status: int = 200,
) -> dict:
    payload = {
        "name": f"Minted {random_lower_string()[:8]}",
        "type": "openai",
        "provisioning_mode": "minted",
        "provider_admin_credential_id": admin_credential_id,
        "target_user_ids": target_user_ids or [],
        "set_as_default": set_as_default,
    }
    if auto_provision_roles is not None:
        payload["auto_provision_roles"] = auto_provision_roles
    response = client.post(f"{MANAGED_BASE}/", headers=headers, json=payload)
    assert response.status_code == expected_status, response.text
    return response.json()


def _record(client: TestClient, headers: dict[str, str], parent_id: str) -> dict:
    response = client.get(f"{MANAGED_BASE}/{parent_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _member(
    client: TestClient, headers: dict[str, str], parent_id: str, user_id: str
) -> dict | None:
    for member in _record(client, headers, parent_id)["members"]:
        if member["user_id"] == user_id:
            return member
    return None


def _user_credentials(client: TestClient, headers: dict[str, str]) -> list[dict]:
    return client.get(f"{API}/ai-credentials/", headers=headers).json()["data"]


def _new_user(client: TestClient) -> dict:
    """A signed-up account plus its own auth headers, in one value."""
    user, headers = create_random_user_with_headers(client)
    user["headers"] = headers
    return user


# ── Shape rules, stated server-side ─────────────────────────────────────────


def test_a_minted_record_is_refused_without_an_admin_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A parent that can never mint must not be creatable.

    Stated on the server, not in the dialog: a rule answered only in a browser is
    answered nowhere, and this one has a second caller already (the invitation
    wizard).
    """
    response = client.post(
        f"{MANAGED_BASE}/",
        headers=superuser_token_headers,
        json={
            "name": "No admin credential",
            "type": "openai",
            "provisioning_mode": "minted",
            "target_user_ids": [],
        },
    )
    assert response.status_code == 400, response.text
    assert "provider admin credential" in str(response.json()["detail"])


def test_anthropic_cannot_be_minted(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Its administration API cannot create keys, so minted mode is refused.

    The refusal is at creation rather than at the first mint, because failing at
    the first mint fails for every member at once and looks like an outage.
    """
    response = client.post(
        f"{MANAGED_BASE}/",
        headers=superuser_token_headers,
        json={
            "name": "Minted anthropic",
            "type": "anthropic",
            "provisioning_mode": "minted",
            "target_user_ids": [],
        },
    )
    assert response.status_code == 400, response.text
    assert "cannot create API keys" in str(response.json()["detail"])


def test_a_minted_record_must_not_be_given_a_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A key here would be stored, never used, and read as 'rotated'."""
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    response = client.post(
        f"{MANAGED_BASE}/",
        headers=superuser_token_headers,
        json={
            "name": "Minted with a key",
            "type": "openai",
            "provisioning_mode": "minted",
            "provider_admin_credential_id": admin_credential_id,
            "api_key": "sk-should-not-be-here",
            "target_user_ids": [],
        },
    )
    assert response.status_code == 400, response.text


def test_a_shared_record_still_requires_a_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``api_key`` became optional on the request model; it is not optional here.

    The field is nullable so that omission is *representable* — a minted record
    genuinely has none. The rule that a shared record must have one moved into the
    service, and this is the test that it moved rather than evaporated.
    """
    response = client.post(
        f"{MANAGED_BASE}/",
        headers=superuser_token_headers,
        json={"name": "Shared, keyless", "type": "openai", "target_user_ids": []},
    )
    assert response.status_code == 400, response.text
    assert "API key is required" in str(response.json()["detail"])


def test_a_minted_record_refuses_key_rotation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Refused, not ignored — silence here reads as a rotation that happened."""
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    parent = _minted_parent(
        client, superuser_token_headers, admin_credential_id=admin_credential_id
    )
    parent_id = parent["record"]["id"]

    response = client.patch(
        f"{MANAGED_BASE}/{parent_id}",
        headers=superuser_token_headers,
        json={"api_key": "sk-rotated"},
    )
    assert response.status_code == 400, response.text
    assert "nothing to rotate" in str(response.json()["detail"])


def test_a_minted_record_reports_that_it_holds_no_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``has_api_key`` is computed now; it used to be the constant ``True``."""
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    parent = _minted_parent(
        client, superuser_token_headers, admin_credential_id=admin_credential_id
    )["record"]
    assert parent["provisioning_mode"] == "minted"
    assert parent["has_api_key"] is False
    assert parent["provider_admin_credential_id"] == admin_credential_id

    # And a shared record still says it holds one.
    shared = client.post(
        f"{MANAGED_BASE}/",
        headers=superuser_token_headers,
        json={
            "name": "Shared",
            "type": "openai",
            "api_key": "sk-shared",
            "target_user_ids": [],
        },
    ).json()["record"]
    assert shared["provisioning_mode"] == "shared"
    assert shared["has_api_key"] is True


# ── Membership before the key ───────────────────────────────────────────────


def test_adding_a_member_records_the_intent_and_contacts_nobody(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The member exists immediately; the key does not, and no provider is called.

    Both halves matter. The member must be visible at once — an admin who added
    somebody and sees nothing assumes it failed — and the provider must not be
    contacted on the request path, because this same code runs inline on signup
    and on the OAuth callback, where a provider timeout would be a failed login.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)

    with stub_minting_providers() as (_probes, provisioning):
        parent = _minted_parent(
            client,
            superuser_token_headers,
            admin_credential_id=admin_credential_id,
            target_user_ids=[str(user["id"])],
        )
    assert provisioning.mint_count == 0, provisioning.calls
    assert not provisioning.calls, "a provider was contacted on the request path"

    parent_id = parent["record"]["id"]
    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member is not None
    assert member["provisioning_status"] == "pending"
    assert member["child_credential_id"] is None
    assert parent["record"]["member_count"] == 1

    # And no credential row was invented for them in the meantime.
    assert _user_credentials(client, user["headers"]) == []


def test_converge_mints_the_key_and_creates_one_usable_credential(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        report = converge_keys(db)

    provisioning.assert_minted(1)
    assert report.provisioned == 1, report

    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provisioning_status"] == "provisioned"
    assert member["child_credential_id"] is not None

    credentials = _user_credentials(client, user["headers"])
    assert len(credentials) == 1
    assert credentials[0]["id"] == member["child_credential_id"]
    assert credentials[0]["is_admin_managed"] is True
    assert credentials[0]["has_api_key"] is True

    # The mint is named per (record, member), so the provider console shows who a
    # service account belongs to.
    create_call = next(
        call for call in provisioning.calls if call.path.endswith("/service_accounts")
    )
    assert str(user["id"]) in create_call.json_body["name"]
    assert parent_id in create_call.json_body["name"]
    # ``scopes`` is deliberately not sent: the vocabulary is not authoritative in
    # anything we hold, and a guessed list hard-fails the create call the day the
    # provider changes it.
    assert "scopes" not in create_call.json_body


def test_a_second_converge_does_nothing(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Idempotence, asserted at the provider rather than at the row.

    A pass that re-minted for an already-provisioned member would leave the first
    key live and unattributed, which the row would not show.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        second = converge_keys(db)

    provisioning.assert_minted(1)
    assert second.attempted == 0, second


def test_set_as_default_wires_the_minted_credential_when_it_arrives(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The default wiring runs at the mint, not at the (keyless) add.

    A member with no credential has nothing to make default. The parent's flag
    still applies, and this is what says it is applied later rather than lost.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
        set_as_default=True,
    )

    with stub_minting_providers():
        converge_keys(db)

    credentials = _user_credentials(client, user["headers"])
    assert len(credentials) == 1
    assert credentials[0]["is_default"] is True


# ── Every way the mint can go wrong ─────────────────────────────────────────


def test_minting_into_a_project_whose_limit_is_not_enforced_is_refused(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The case a naive implementation passes.

    A real ``threshold_amount`` is present; enforcement is ``inactive``. Anything
    reading the obvious field calls that capped. The mint must not happen, and the
    refusal must come *before* a key exists — there is deliberately no
    mint-then-cap path.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers(
        enforcement_status="inactive", threshold_cents=5000
    ) as (_probes, provisioning):
        converge_keys(db)

    provisioning.assert_minted(0)
    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provisioning_status"] == "pending"
    assert member["provision_error"] == "project_not_capped"
    assert _user_credentials(client, user["headers"]) == []


def test_a_null_secret_is_a_failure_not_an_empty_key(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``api_key`` is nullable in the provider's own schema.

    An empty key stored on a credential row is indistinguishable from a real one
    until somebody uses it, which is the failure this whole design is arranged to
    make impossible. So a null secret is a mint failure and no credential appears.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers(omit_secret=True):
        converge_keys(db)

    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provision_error"] == "no_secret_returned"
    assert member["child_credential_id"] is None
    assert _user_credentials(client, user["headers"]) == []


def test_retries_are_bounded_and_end_in_a_durable_failure(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Bounded, converging, and never cleaned up.

    The point of the ceiling is that somebody eventually has to look: a row that
    retries forever with an ever-later next attempt reads as "still working" on
    every surface that shows it. And the failed member stays a **member** — the
    row is not deleted to tidy up, because a failure nobody can see afterwards
    makes "account creation never fails because provisioning failed" a lie.
    """
    from app.services.credentials.key_provisioning_service import (
        key_provisioning_service,
    )

    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers(mint_error="provider_error"):
        for attempt in range(1, key_provisioning_service.MAX_ATTEMPTS + 1):
            # Each pass is due immediately only because the previous one is
            # forced back to due; the backoff itself is asserted below.
            make_membership_due(db, user["id"])
            converge_keys(db)
            member = _member(
                client, superuser_token_headers, parent_id, str(user["id"])
            )
            assert member["provision_attempts"] == attempt

    assert member["provisioning_status"] == "failed"
    assert member["provision_error"] == "provider_error"

    # Terminal: a further pass does not pick it up again.
    with stub_minting_providers(mint_error="provider_error") as (_p, provisioning):
        report = converge_keys(db)
    assert report.attempted == 0, report
    assert provisioning.mint_count == 0

    # And the failure is still a member, visible to the admin who must fix it.
    assert (
        _member(client, superuser_token_headers, parent_id, str(user["id"]))
        is not None
    )


def test_a_backoff_defers_the_next_attempt(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A failed member is not retried on the very next tick."""
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )

    with stub_minting_providers(mint_error="provider_error") as (_p, provisioning):
        first = converge_keys(db)
        second = converge_keys(db)

    assert first.attempted == 1
    assert second.attempted == 0, "the backoff was not respected"
    assert provisioning.mint_count == 0


def test_a_key_minted_before_a_crash_is_revoked_before_the_next_attempt(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The crash window, and why the handles are committed before the key.

    The provider creates the service account and returns the secret in one call;
    the handles are stored in their own commit *before* the key is. So a process
    that dies between them leaves a row that names the orphan it created — and
    the next attempt destroys that orphan before minting again, which is what
    turns a crash into a wasted key rather than a leaked one.

    The crash is simulated at the database, aborting the child credential's
    INSERT, so what the code experiences is a real aborted transaction rather than
    a tidy Python exception.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        with failing_sql_statement(
            db, when_statement_contains=CHILD_CREDENTIAL_INSERT
        ) as injected:
            converge_keys(db)
        assert injected["fired"], "the injection never fired — nothing was proved"

        # The key exists at the provider and we have no credential for it.
        provisioning.assert_minted(1)
        member = _member(
            client, superuser_token_headers, parent_id, str(user["id"])
        )
        assert member["child_credential_id"] is None
        assert member["provisioning_status"] == "pending"
        assert _user_credentials(client, user["headers"]) == []

        make_membership_due(db, user["id"])
        converge_keys(db)

    # The orphan is destroyed first, then a fresh key is minted.
    provisioning.assert_revoked("svc_1")
    provisioning.assert_minted(2)
    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provisioning_status"] == "provisioned"
    assert len(_user_credentials(client, user["headers"])) == 1


# ── Removal, deactivation, deletion ─────────────────────────────────────────


def test_removing_a_member_deletes_the_credential_and_then_revokes_the_key(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Delete first, revoke second — the ordering is the point.

    The delete can be refused (a published bundle's publisher credential is
    Tier-2 blocked). A revoke that ran first would leave a dead key on a surviving
    row that reads as healthy everywhere and fails at first use; this way a
    blocked member keeps a working key, which is recoverable.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        assert len(_user_credentials(client, user["headers"])) == 1

        removed = client.patch(
            f"{MANAGED_BASE}/{parent_id}",
            headers=superuser_token_headers,
            json={"target_user_ids": []},
        )
        assert removed.status_code == 200, removed.text
        # The database side is done by the time the request returns; the provider
        # call is deliberately not, so an admin's PATCH is not as slow as the
        # slowest provider call in the batch.
        assert provisioning.revoke_count == 0
        assert _user_credentials(client, user["headers"]) == []
        assert _member(client, superuser_token_headers, parent_id, str(user["id"])) is None

        drain_tasks()

    provisioning.assert_revoked("svc_1")


def test_deactivating_an_account_revokes_its_minted_key_and_keeps_the_membership(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The membership survives as ``suspended``; reactivation mints a fresh key.

    Not left ``pending``: a pending row on a disabled account reads as work in
    progress for as long as the account stays disabled, which is the "infinite
    backoff that looks like progress" this design refuses.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)

        deactivated = client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": False},
        )
        assert deactivated.status_code == 200, deactivated.text
        drain_tasks()

        provisioning.assert_revoked("svc_1")
        member = _member(
            client, superuser_token_headers, parent_id, str(user["id"])
        )
        assert member["provisioning_status"] == "suspended"
        assert member["child_credential_id"] is None

        # A converge pass leaves a suspended member alone.
        assert converge_keys(db).attempted == 0

        reactivated = client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": True},
        )
        assert reactivated.status_code == 200, reactivated.text
        assert (
            _member(client, superuser_token_headers, parent_id, str(user["id"]))[
                "provisioning_status"
            ]
            == "pending"
        )

        converge_keys(db)

    provisioning.assert_minted(2)
    assert (
        _member(client, superuser_token_headers, parent_id, str(user["id"]))[
            "provisioning_status"
        ]
        == "provisioned"
    )


def test_a_patch_that_says_nothing_about_is_active_revokes_nothing(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``is_active`` is a plain bool defaulting to True.

    A truthiness check would read every PATCH that says nothing about it as
    "activate", and — worse for the mirror case — a naive "did the value change"
    on an unset field would compare against the default. This is the test that the
    transition is detected from ``exclude_unset`` and a before-snapshot.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        renamed = client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"full_name": "Renamed Person"},
        )
        assert renamed.status_code == 200, renamed.text
        drain_tasks()

    assert provisioning.revoke_count == 0
    assert (
        _member(client, superuser_token_headers, parent_id, str(user["id"]))[
            "provisioning_status"
        ]
        == "provisioned"
    )
    assert len(_user_credentials(client, user["headers"])) == 1


def test_deleting_an_account_revokes_its_minted_keys(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The handles are read before the cascade takes them.

    ``DELETE /users/{id}`` is a bare ``session.delete(user)``; the membership rows
    go with it. Reading the handles afterwards is not possible, and a key nobody
    can name is a key nobody can destroy.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        deleted = client.delete(
            f"{API}/users/{user['id']}", headers=superuser_token_headers
        )
        assert deleted.status_code == 200, deleted.text
        drain_tasks()

    provisioning.assert_revoked("svc_1")


def test_disconnecting_a_provider_organisation_is_refused_while_keys_live(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """This secret is the only way to destroy the keys it minted.

    Deleting it does not merely stop new mints; it strands every existing key at
    the provider forever. So the delete is refused, with the counts that make the
    consequence legible, and forcing it is an explicit act.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )

    with stub_minting_providers():
        converge_keys(db)

    refused = client.delete(
        f"{ADMIN_CRED_BASE}/{admin_credential_id}", headers=superuser_token_headers
    )
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["minting_credential_count"] == 1
    assert detail["live_minted_key_count"] == 1

    forced = client.delete(
        f"{ADMIN_CRED_BASE}/{admin_credential_id}?force=true",
        headers=superuser_token_headers,
    )
    assert forced.status_code == 200, forced.text


# ── The member-list projection ──────────────────────────────────────────────


def test_the_member_list_costs_the_same_however_many_members_there_are(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A fixed number of queries, not one per member.

    The projection reads two tables now, which is exactly where an N+1 hides —
    and it matters more than it used to, because a minted record's members appear
    the moment they are added, so an admin watching a batch provision is looking
    at this page while it is at its largest.

    Paired with the behavioural assertion, as the counter's own docstring insists:
    a count alone passes just as happily when the feature stops working.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    users = [_new_user(client) for _ in range(4)]
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(users[0]["id"])],
    )
    parent_id = parent["record"]["id"]

    with count_queries(db, matching="managed_ai_credential_membership") as one:
        first = _record(client, superuser_token_headers, parent_id)
    assert first["member_count"] == 1
    # A nonzero anchor, because ``len(many) == len(one)`` is satisfied by
    # ``0 == 0``: the day the ``matching=`` string drifts away from the SQL
    # SQLAlchemy emits, this test counts nothing and passes for it.
    assert one, "the needle matched no statements — this test counts nothing"

    with count_queries(db, matching="ai_credential.is_default") as one_predicate:
        _record(client, superuser_token_headers, parent_id)
    assert one_predicate, (
        "the onboarding predicate did not run — the member projection is no "
        "longer reading it, or the needle drifted"
    )

    client.patch(
        f"{MANAGED_BASE}/{parent_id}",
        headers=superuser_token_headers,
        json={"target_user_ids": [str(u["id"]) for u in users]},
    )

    with count_queries(db, matching="managed_ai_credential_membership") as many:
        second = _record(client, superuser_token_headers, parent_id)
    assert second["member_count"] == 4
    assert {m["provisioning_status"] for m in second["members"]} == {"pending"}
    assert len(many) == len(one), (one, many)

    # **The second table, and it is the one an N+1 hid in.** The count above
    # filters ``managed_ai_credential_membership`` alone, so the onboarding
    # predicate's other half — one ``SELECT ... FROM ai_credential WHERE
    # is_default`` per person — was invisible to it. It is one query for the
    # whole list, whatever the list is.
    with count_queries(db, matching="ai_credential.is_default") as many_predicate:
        _record(client, superuser_token_headers, parent_id)
    assert len(many_predicate) == len(one_predicate), (
        one_predicate,
        many_predicate,
    )


def test_adding_members_asks_the_onboarding_predicate_once_for_the_batch(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The N+1 was on the **write** path, where nothing was counting.

    The projection has had a query-count test since it was written. The three
    callers that *build* member DTOs did not, and all three called
    ``_owner_key_states(session, [owner_id])`` — two queries — inside a per-owner
    loop, so an admin PATCH adding two hundred members issued four hundred extra
    queries on a request path. ``_member_dto`` requiring ``key_state`` as a
    keyword is what stops a wrong default; it does nothing about a right answer
    fetched once per member.

    One member and four members must cost the same number of onboarding-predicate
    queries, because it is an account-wide question asked about a list.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    users = [_new_user(client) for _ in range(4)]
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[],
    )
    parent_id = parent["record"]["id"]

    with count_queries(db, matching="ai_credential.is_default") as one:
        added_one = client.patch(
            f"{MANAGED_BASE}/{parent_id}",
            headers=superuser_token_headers,
            json={"target_user_ids": [str(users[0]["id"])]},
        )
    assert added_one.status_code == 200, added_one.text
    assert len(added_one.json()["added"]) == 1
    assert one, "the needle matched nothing — this test counts nothing"

    with count_queries(db, matching="ai_credential.is_default") as many:
        added_many = client.patch(
            f"{MANAGED_BASE}/{parent_id}",
            headers=superuser_token_headers,
            json={"target_user_ids": [str(u["id"]) for u in users]},
        )
    assert added_many.status_code == 200, added_many.text
    # The behavioural half, as the counter's own docstring insists: a count
    # alone passes just as happily when the feature stops working.
    assert len(added_many.json()["added"]) == 3
    assert {m["api_key_onboarding_state"] for m in added_many.json()["added"]} == {
        "preparing"
    }
    assert len(many) == len(one), (one, many)


def test_a_shared_member_reports_an_explicit_not_applicable_status(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Never a null, and never inferred from the absence of provisioning state.

    A shared member has nothing to provision, and saying so explicitly is what
    stops a client from deciding for itself what a missing status means.
    """
    user = _new_user(client)
    shared = client.post(
        f"{MANAGED_BASE}/",
        headers=superuser_token_headers,
        json={
            "name": "Shared record",
            "type": "openai",
            "api_key": "sk-shared",
            "target_user_ids": [str(user["id"])],
        },
    ).json()
    member = shared["added"][0]
    assert member["provisioning_status"] == "not_applicable"
    assert member["child_credential_id"] is not None

    from_record = _member(
        client, superuser_token_headers, shared["record"]["id"], str(user["id"])
    )
    assert from_record["provisioning_status"] == "not_applicable"


def test_deactivating_a_shared_member_revokes_nothing(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """One key held by many people must not die because one holder left."""
    user = _new_user(client)
    shared = client.post(
        f"{MANAGED_BASE}/",
        headers=superuser_token_headers,
        json={
            "name": "Shared record",
            "type": "openai",
            "api_key": "sk-shared",
            "target_user_ids": [str(user["id"])],
        },
    ).json()
    parent_id = shared["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": False},
        )
        drain_tasks()

    assert provisioning.revoke_count == 0
    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provisioning_status"] == "not_applicable"
    assert member["child_credential_id"] is not None


def test_a_record_wired_to_a_deleted_admin_credential_converges_to_failed(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The branch that never claimed an attempt, and so never ended.

    Forcing a provider organisation's disconnect NULLs
    ``provider_admin_credential_id`` on every record that minted through it. The
    memberships underneath then have nothing to mint with — and if that branch
    records a failure *without* claiming an attempt, it never reaches the ceiling:
    the row sits at ``pending`` retrying every minute forever, and writes one
    ``mint_failed`` security event per minute into its owner's feed while doing
    it. Bounded means bounded on **every** path out of an attempt.
    """
    from app.services.credentials.key_provisioning_service import (
        key_provisioning_service,
    )

    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    forced = client.delete(
        f"{ADMIN_CRED_BASE}/{admin_credential_id}?force=true",
        headers=superuser_token_headers,
    )
    assert forced.status_code == 200, forced.text

    with stub_minting_providers() as (_probes, provisioning):
        for attempt in range(1, key_provisioning_service.MAX_ATTEMPTS + 1):
            make_membership_due(db, user["id"])
            converge_keys(db)
            member = _member(
                client, superuser_token_headers, parent_id, str(user["id"])
            )
            assert member["provision_attempts"] == attempt, member

    assert member["provisioning_status"] == "failed"
    assert member["provision_error"] == "no_admin_credential"
    assert provisioning.mint_count == 0

    # Terminal: no further pass picks it up, so no further event is written.
    with stub_minting_providers():
        assert converge_keys(db).attempted == 0


def test_a_failed_member_can_be_retried_and_re_adding_them_cannot(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Terminal must not mean unreachable — but the way out has to be asked for.

    Re-adding the member is deliberately *not* the retry: reconcile passes the
    current membership list as the desired one, so a PATCH that merely renames the
    record would otherwise reset every durable failure it touched. The retry is
    its own explicit, audited action.
    """
    from app.services.credentials.key_provisioning_service import (
        key_provisioning_service,
    )

    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers(mint_error="provider_error"):
        for _ in range(key_provisioning_service.MAX_ATTEMPTS):
            make_membership_due(db, user["id"])
            converge_keys(db)
    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provisioning_status"] == "failed"

    # Re-adding does nothing, and must not: it is how an unrelated PATCH would
    # silently reset the failure.
    readded = client.patch(
        f"{MANAGED_BASE}/{parent_id}",
        headers=superuser_token_headers,
        json={"target_user_ids": [str(user["id"])]},
    )
    assert readded.status_code == 200, readded.text
    assert (
        _member(client, superuser_token_headers, parent_id, str(user["id"]))[
            "provisioning_status"
        ]
        == "failed"
    )

    retried = client.post(
        f"{MANAGED_BASE}/{parent_id}/members/{user['id']}/retry",
        headers=superuser_token_headers,
    )
    assert retried.status_code == 200, retried.text
    requeued = _member(
        client, superuser_token_headers, parent_id, str(user["id"])
    )
    assert requeued["provisioning_status"] == "pending"
    assert requeued["provision_attempts"] == 0
    # The reason it failed last time survives the requeue — until it succeeds,
    # that is still the most useful thing on the row.
    assert requeued["provision_error"] == "provider_error"

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
    provisioning.assert_minted(1)
    assert (
        _member(client, superuser_token_headers, parent_id, str(user["id"]))[
            "provisioning_status"
        ]
        == "provisioned"
    )


def test_retrying_a_member_who_has_not_failed_is_refused(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Retry is for a terminal failure, not a general re-mint button."""
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    other = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    pending = client.post(
        f"{MANAGED_BASE}/{parent_id}/members/{user['id']}/retry",
        headers=superuser_token_headers,
    )
    assert pending.status_code == 400, pending.text

    not_a_member = client.post(
        f"{MANAGED_BASE}/{parent_id}/members/{other['id']}/retry",
        headers=superuser_token_headers,
    )
    assert not_a_member.status_code == 404, not_a_member.text


def test_deactivation_revokes_a_live_key_held_by_a_terminally_failed_member(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``failed`` does not mean keyless, and deactivation must not assume it does.

    This is the state the premise got wrong. Neither ``_settle`` nor
    ``_record_failure`` clears ``external_key_ref``, and ``child_create_failed``
    reaches the attempt ceiling with it populated — the provider made a key, we
    could not store it, and we gave up. Skipping ``failed`` on deactivation left
    that key live at the provider forever.

    Note what it is *not*: the row keeps its status, its error and its attempt
    count. Revoking the key and erasing the record of why it failed are separate
    things, and only the first belongs to an account action.
    """
    from app.services.credentials.key_provisioning_service import (
        key_provisioning_service,
    )

    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        # Every attempt mints, records the handles, then fails to write the
        # credential row — so the ceiling is reached with a key we own.
        for _ in range(key_provisioning_service.MAX_ATTEMPTS):
            make_membership_due(db, user["id"])
            with failing_sql_statement(
                db, when_statement_contains=CHILD_CREDENTIAL_INSERT
            ) as injected:
                converge_keys(db)
            assert injected["fired"], "the injection never fired — nothing was proved"

        member = _member(
            client, superuser_token_headers, parent_id, str(user["id"])
        )
        assert member["provisioning_status"] == "failed"
        assert member["provision_error"] == "child_create_failed"
        # Each attempt destroyed the previous attempt's orphan before minting
        # again, so exactly one key is still live: the last one.
        minted_count = provisioning.mint_count
        assert minted_count == key_provisioning_service.MAX_ATTEMPTS
        assert provisioning.revoke_count == minted_count - 1
        live_key = f"svc_{minted_count}"

        deactivated = client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": False},
        )
        assert deactivated.status_code == 200, deactivated.text
        drain_tasks()

        # The key that was still live is destroyed…
        assert provisioning.revoked[-1] == live_key
        assert provisioning.revoke_count == minted_count

        # …and the durable failure is left exactly as it was.
        member = _member(
            client, superuser_token_headers, parent_id, str(user["id"])
        )
        assert member["provisioning_status"] == "failed"
        assert member["provision_error"] == "child_create_failed"
        assert member["provision_attempts"] == key_provisioning_service.MAX_ATTEMPTS

        # The handles are gone with the key, so nothing tries to revoke it a
        # second time. This is the assertion that the ref was actually cleared —
        # the member projection deliberately does not expose it.
        client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": True},
        )
        client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": False},
        )
        drain_tasks()
        assert provisioning.revoke_count == minted_count


def test_deactivation_revokes_nothing_for_a_failure_that_never_got_a_key(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The other half of the same rule, driven by the state where it is true.

    ``mint_error`` never reaches the point of writing handles, so this member
    genuinely holds nothing and there is nothing to destroy. Kept as its own
    test because this used to be the *only* test of deactivation-versus-failed:
    it asserted ``revoke_count == 0`` while driving the one failure mode where
    the ref is never written, so it was green for the case it was not testing.
    """
    from app.services.credentials.key_provisioning_service import (
        key_provisioning_service,
    )

    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers(mint_error="provider_error") as (_p, minting):
        for _ in range(key_provisioning_service.MAX_ATTEMPTS):
            make_membership_due(db, user["id"])
            converge_keys(db)
        assert minting.mint_count == 0, "no key should ever have been created"

    with stub_minting_providers() as (_probes, provisioning):
        client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": False},
        )
        drain_tasks()

    assert provisioning.revoke_count == 0
    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provisioning_status"] == "failed"
    assert member["provision_error"] == "provider_error"
    assert member["provision_attempts"] == key_provisioning_service.MAX_ATTEMPTS


def test_a_member_cannot_be_removed_while_their_mint_is_in_flight(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The window an admin can open, as opposed to the one a crash can.

    A converge pass claims the row and then sits inside a provider call. If the
    membership is deleted during that call, the handles the mint is about to
    write land nowhere: the service account exists, no row names it, and no
    revocation can ever be scheduled. Refusing the removal for the length of one
    converge tick costs the admin a retry and costs nobody a key.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    mark_membership_minting(db, user["id"])

    response = client.patch(
        f"{MANAGED_BASE}/{parent_id}",
        headers=superuser_token_headers,
        json={"target_user_ids": []},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [b["reason"] for b in body["blocked"]] == ["mint_in_flight"]
    assert body["removed"] == []
    assert (
        _member(client, superuser_token_headers, parent_id, str(user["id"]))
        is not None
    )

    # The block carries the server's own sentence, and it must be about the
    # mint. Three clients used to substitute "in use by a published bundle" for
    # every reason, which pointed an administrator blocked by an in-flight mint
    # straight at the force delete — the one action that must not be taken while
    # a key is being created.
    message = body["blocked"][0]["message"]
    assert "being created" in message
    assert "bundle" not in message.lower()

    # And the delete route's 409 says the same thing, from the same source.
    refused = client.delete(
        f"{MANAGED_BASE}/{parent_id}", headers=superuser_token_headers
    )
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert message in detail["message"]
    assert "in use by a published bundle" not in detail["message"]
    assert detail["blocked"][0]["reason"] == "mint_in_flight"


def test_removing_a_member_records_the_revoke_in_their_own_feed(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The busiest revocation of all, and it must not be the unaudited one.

    The audit target is a required field precisely because it was defaulted once:
    the escape hatch for "the account is being deleted, there is no feed" was
    inherited by the path where the holder is very much still there.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        client.patch(
            f"{MANAGED_BASE}/{parent_id}",
            headers=superuser_token_headers,
            json={"target_user_ids": []},
        )
        drain_tasks()

    provisioning.assert_revoked("svc_1")
    events = client.get(
        f"{API}/security-events/",
        headers=user["headers"],
        params={"event_type": "admin.ai_credential.revoked"},
    ).json()["data"]
    assert len(events) == 1, events
    # The external ref is recorded on purpose: it is what makes a key nameable
    # after its row is gone. ``api_key_id`` is a handle and belongs here; the
    # secret itself never appears anywhere.
    ref = events[0]["details"]["external_key_ref"]
    assert ref["service_account_id"] == "svc_1"
    assert ref["api_key_id"] == "key_1"
    assert "sk-proj-minted" not in str(events[0]["details"])


def test_re_inviting_an_interrupted_invite_as_inactive_revokes_its_minted_key(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The third ``is_active`` write site, and the one nobody thinks to look for.

    ``on_account_deactivated``'s docstring names its callers, which makes it a
    checkable claim — and the first audit of it found this one missing. Re-inviting
    deactivates through ``InvitationService``, not through ``update_user``, so
    without the wiring the account would be disabled while its minted key stayed
    live at the provider and its credential row stayed in the table.

    The setup is the narrow shape a re-invite actually adopts rather than refusing
    as a duplicate: an account row an earlier invite committed before failing to
    write its invitation. It still went through the creation chokepoint, so
    auto-provisioning still gave it a membership — which is exactly why this path
    can hold a minted key at all.
    """
    from tests.utils.invitation import invite_user

    admin_credential_id = _admin_credential(client, superuser_token_headers)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        auto_provision_roles=["agent-user"],
    )
    parent_id = parent["record"]["id"]

    email = random_email()
    with failing_sql_statement(
        db, when_statement_contains="INSERT INTO user_invitation"
    ) as injected:
        with pytest.raises(Exception):
            client.post(
                f"{API}/users/invite",
                headers=superuser_token_headers,
                json={"email": email, "role": "agent-user", "send_email": False},
            )
    assert injected["fired"], "the injection never fired — nothing was proved"
    restore_session(db)

    member = _record(client, superuser_token_headers, parent_id)["members"][0]
    user_id = member["user_id"]
    assert member["provisioning_status"] == "pending"

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        assert (
            _member(client, superuser_token_headers, parent_id, user_id)[
                "provisioning_status"
            ]
            == "provisioned"
        )

        invite_user(
            client,
            superuser_token_headers,
            email=email,
            role="agent-user",
            is_active=False,
        )
        drain_tasks()

    provisioning.assert_revoked("svc_1")
    after = _member(client, superuser_token_headers, parent_id, user_id)
    assert after["provisioning_status"] == "suspended"
    assert after["child_credential_id"] is None


def test_deleting_a_minted_record_revokes_every_members_key(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Deleting the record destroys the keys it minted, in the right order.

    The reconcile-to-empty inside ``delete`` removes each member and schedules
    their revoke; the parent row goes afterwards. The reason the parent delete
    also sweeps for survivors is that ``force`` gets past the blast-radius gate
    but not past a lock timeout — and once the parent is gone, the cascade has
    taken the handles with it.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    users = [_new_user(client) for _ in range(2)]
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(u["id"]) for u in users],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        provisioning.assert_minted(2)

        deleted = client.delete(
            f"{MANAGED_BASE}/{parent_id}", headers=superuser_token_headers
        )
        assert deleted.status_code == 200, deleted.text
        drain_tasks()

    assert sorted(provisioning.revoked) == ["svc_1", "svc_2"]
    for user in users:
        assert _user_credentials(client, user["headers"]) == []


# ── The claim: what happens when the row stops being the mint's to write ─────
#
# Three tests, one for each way a request handler can land inside the provider
# await. They are not variations on a theme: the row ends up in a *different*
# state in each (suspended, cascaded away with its user, cascaded away with its
# parent), and the only thing that must be identical is that the key the
# provider created is destroyed rather than left live with nothing naming it.
#
# **Only the first of the three exercises the claim token itself**, and saying so
# matters because these were once described as three tests of it. In the two
# deletion cases the row is CASCADE-deleted, so an id-only conditional update
# passes them just as happily — what they cover is ``_discard_orphan_key``, not
# the ``status``/``updated_at`` predicate. The deactivation case is the one where
# the row still exists and has been settled by somebody else, which is the only
# shape a weaker predicate gets wrong. A fourth case — a mint that *fails* while
# the same deactivation is running — is further down, under "The failure path is
# claimed too"; the failure write is the one with the sharper consequence.


def test_deactivating_an_account_mid_mint_revokes_the_key_it_could_not_store(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The deactivation wins the row; the mint destroys what it minted.

    Without the claim token this is the worst outcome in the feature: the
    deactivation sees ``external_key_ref IS NULL`` (it has not been written yet),
    queues no revocation and writes ``suspended`` — and then the mint writes the
    ref, creates the credential and overwrites the status with ``provisioned``.
    A disabled account ends up holding a live provider key and a usable
    credential, and reactivating them later finds nothing suspended to resume.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    def deactivate() -> None:
        response = client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": False},
        )
        assert response.status_code == 200, response.text

    with stub_minting_providers(on_mint=deactivate) as (_probes, provisioning):
        report = converge_keys(db)
        drain_tasks()

    # The key was created and then destroyed. Both halves matter: a test that
    # only asserted the second would pass if the mint had never happened.
    provisioning.assert_minted(1)
    provisioning.assert_revoked("svc_1")
    assert "claim_lost" in report.skipped
    assert report.provisioned == 0

    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provisioning_status"] == "suspended"
    assert member["child_credential_id"] is None

    # And the suspension is real, so reactivation still has something to resume.
    # (Their own credential list can only be read once they are active again —
    # a deactivated account cannot authenticate, which is the point of it.)
    reactivated = client.patch(
        f"{API}/users/{user['id']}",
        headers=superuser_token_headers,
        json={"is_active": True},
    )
    assert reactivated.status_code == 200, reactivated.text
    assert (
        _member(client, superuser_token_headers, parent_id, str(user["id"]))[
            "provisioning_status"
        ]
        == "pending"
    )
    assert _user_credentials(client, user["headers"]) == []


def test_deleting_an_account_mid_mint_revokes_the_key_it_could_not_store(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The membership cascades away with the user; the key must not survive it.

    ``collect_user_revocations`` filters on ``external_key_ref IS NOT NULL`` and
    the ref is still null at this instant, so the deletion path cannot know about
    this key — nothing it does can help. The attempt that minted it is the only
    thing that can, which is why a lost claim revokes rather than logs.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )

    def delete_account() -> None:
        response = client.delete(
            f"{API}/users/{user['id']}", headers=superuser_token_headers
        )
        assert response.status_code == 200, response.text

    with stub_minting_providers(on_mint=delete_account) as (_probes, provisioning):
        report = converge_keys(db)
        drain_tasks()

    provisioning.assert_minted(1)
    provisioning.assert_revoked("svc_1")
    assert "claim_lost" in report.skipped
    assert report.provisioned == 0


def test_force_deleting_the_record_mid_mint_revokes_the_key_it_could_not_store(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``force=true`` walks past the ``mint_in_flight`` block. It must still be safe.

    The Remove pass refuses to remove a member whose mint is in flight, and that
    refusal is unconditional — but ``delete(force=True)`` does not go through it,
    and the ``stranded`` list it reads to salvage keys reads a ref that is still
    null. So the parent, the membership and the handles all disappear while the
    provider call is still open. The claim is what catches it.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    def force_delete_record() -> None:
        response = client.delete(
            f"{MANAGED_BASE}/{parent_id}?force=true",
            headers=superuser_token_headers,
        )
        assert response.status_code == 200, response.text

    with stub_minting_providers(on_mint=force_delete_record) as (
        _probes,
        provisioning,
    ):
        report = converge_keys(db)
        drain_tasks()

    provisioning.assert_minted(1)
    provisioning.assert_revoked("svc_1")
    assert "claim_lost" in report.skipped
    assert report.provisioned == 0
    assert _user_credentials(client, user["headers"]) == []


# ── The onboarding state the dashboard gates on ─────────────────────────────
#
# Nothing tested this field at all, which is how it came to be scoped to a
# provider whose administration API cannot mint: every successful mint resolved
# straight back to ``needs_key`` and the transition below was unreachable.


def _onboarding_state(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get(
        f"{API}/users/me/ai-credentials/status", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["api_key_onboarding_state"]


def test_a_minted_openai_key_takes_the_wall_down(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``preparing → has_key`` — the transition the whole state exists for.

    OpenAI is the only provider this platform can mint from, so a state that
    only Anthropic could satisfy meant the transition never happened once, ever.
    A person auto-provisioned a key would start working and then have the entire
    dashboard replaced by the paste-a-key wall on the next poll.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)

    assert _onboarding_state(client, user["headers"]) == "needs_key"

    _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
        set_as_default=True,
    )
    assert _onboarding_state(client, user["headers"]) == "preparing"

    with stub_minting_providers():
        converge_keys(db)

    assert _onboarding_state(client, user["headers"]) == "has_key"


def test_a_key_that_is_not_their_default_does_not_take_the_wall_down(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """And the administrator is told exactly that, from the same field.

    ``set_as_default`` defaults to false, so an admin can create a perfectly
    good credential and leave the person on the wall with that credential
    visible in their settings. The admin's member projection carries the same
    ``api_key_onboarding_state`` the person's own dashboard reads, so the
    success message cannot claim more than is true.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
        set_as_default=False,
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers():
        converge_keys(db)

    # The credential exists and is theirs…
    assert len(_user_credentials(client, user["headers"])) == 1
    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member["provisioning_status"] == "provisioned"

    # …and nothing will pick it up, which both sides now say.
    assert _onboarding_state(client, user["headers"]) == "needs_key"
    assert member["api_key_onboarding_state"] == "needs_key"

    # Making it their default settles both answers at once.
    credential_id = _user_credentials(client, user["headers"])[0]["id"]
    made_default = client.post(
        f"{API}/ai-credentials/{credential_id}/set-default",
        headers=user["headers"],
    )
    assert made_default.status_code == 200, made_default.text
    assert _onboarding_state(client, user["headers"]) == "has_key"
    assert (
        _member(client, superuser_token_headers, parent_id, str(user["id"]))[
            "api_key_onboarding_state"
        ]
        == "has_key"
    )


def test_the_state_is_not_scoped_to_one_provider(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Any default credential satisfies it, because any of them can run an agent.

    An environment requires a default of the type **its own SDK** expects, and
    every credential type is served by some SDK the user can pick — nothing in
    that path requires Anthropic. Pinned per type so a future narrowing has to
    argue with a test rather than with a comment.
    """
    for cred_type, extra in (
        ("openai", {}),
        ("google", {}),
        ("openai_compatible", {"base_url": "https://example.invalid/v1", "model": "m"}),
    ):
        user = _new_user(client)
        assert _onboarding_state(client, user["headers"]) == "needs_key"
        created = client.post(
            f"{API}/ai-credentials/",
            headers=user["headers"],
            json={
                "name": f"{cred_type} key",
                "type": cred_type,
                "api_key": f"sk-{cred_type}-{random_lower_string()[:8]}",
                **extra,
            },
        )
        assert created.status_code == 200, created.text
        client.post(
            f"{API}/ai-credentials/{created.json()['id']}/set-default",
            headers=user["headers"],
        )
        assert _onboarding_state(client, user["headers"]) == "has_key", (
            f"a default {cred_type} credential must satisfy the wall"
        )


def test_every_block_reason_the_service_can_produce_has_a_sentence() -> None:
    """The reason table and the code that produces reasons must not drift.

    ``ManagedReconcileBlock.of`` falls back to a generic sentence for a reason
    it does not know, which is the right runtime behaviour and the wrong thing
    to discover in production: a new block site would ship a message that says
    nothing, and a message that says nothing is what the three clients filled in
    with a constant of their own in the first place.

    Scanned from the source rather than asserted against a list, so adding a
    fourth ``reason=`` fails here rather than at whatever surface renders it.

    **Over the whole service tree, not one file.** The reason table's docstring
    names this test as the enforcement of its invariant, and an enforcement that
    reads a single named module is only as true as that module staying the only
    place blocks are produced — which is the assumption every scan of this shape
    has eventually been wrong about.
    """
    import ast
    import pathlib

    from app.models.credentials.managed_ai_credential import (
        MANAGED_RECONCILE_BLOCK_MESSAGES,
    )

    services = pathlib.Path("app/services")
    assert services.is_dir(), f"{services} moved; this scan is now vacuous"

    produced: set[str] = set()
    scanned_files = 0
    for source in sorted(services.rglob("*.py")):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "of"):
                continue
            target = func.value
            if not (
                isinstance(target, ast.Name)
                and target.id == "ManagedReconcileBlock"
            ):
                continue
            scanned_files += 1
            for keyword in node.keywords:
                if keyword.arg == "reason" and isinstance(
                    keyword.value, ast.Constant
                ):
                    produced.add(keyword.value.value)

    assert scanned_files, "the scan found no block sites — it is no longer scanning"
    assert produced, "block sites were found but no literal reason — scan broken"
    missing = produced - set(MANAGED_RECONCILE_BLOCK_MESSAGES)
    assert not missing, (
        f"block reasons with no sentence in MANAGED_RECONCILE_BLOCK_MESSAGES: "
        f"{sorted(missing)}. State what the reason means where the reason is "
        f"declared; do not let a client invent it."
    )
    unused = set(MANAGED_RECONCILE_BLOCK_MESSAGES) - produced
    assert not unused, (
        f"sentences for reasons nothing produces: {sorted(unused)}. A stale "
        f"entry reads as a state the system can reach and cannot."
    )


# ── The key each person actually got ────────────────────────────────────────


def test_each_member_holds_the_key_that_was_minted_for_them(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Two members, two keys, and the mapping between them is asserted.

    **The gap this closes is that nothing ever read a minted secret back.** The
    stub mints distinguishable values (``sk-proj-minted-1``, ``sk-proj-minted-2``)
    and every test until now asserted only that *a* credential appeared. So
    ``materialise_minted_child`` storing A's key on B's row, or storing A's
    ``external_key_ref`` against B's membership, would have left the entire suite
    green — while one person held another person's provider key, billed to
    another person's service account, and revoking either one would have
    destroyed the wrong key.

    The mapping is not assumed from mint order. Each user's own security feed
    carries the ``admin.ai_credential.minted`` event naming the external ref the
    server recorded *for them*, and the account-config bundle hands that same
    user their decrypted key. The two have to agree, per user.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    users = [_new_user(client) for _ in range(2)]
    _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(u["id"]) for u in users],
    )

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        provisioning.assert_minted(2)

    seen_secrets: set[str] = set()
    for user in users:
        events = client.get(
            f"{API}/security-events/",
            headers=user["headers"],
            params={"event_type": "admin.ai_credential.minted"},
        ).json()["data"]
        assert len(events) == 1, (user["email"], events)
        details = events[0]["details"]
        assert details["target_user_id"] == str(user["id"])
        ref = details["external_key_ref"]
        # ``svc_N`` / ``key_N`` share the stub's index, and the secret for that
        # index is ``sk-proj-minted-N``. That index is the ground truth for
        # "which key was created for this person".
        index = ref["service_account_id"].removeprefix("svc_")
        assert ref["api_key_id"] == f"key_{index}", ref
        expected_secret = f"sk-proj-minted-{index}"

        # The one endpoint that returns a decrypted key, and it returns only the
        # caller's own — so reaching it as this user is itself part of the
        # assertion that the credential landed on the right account.
        desktop = obtain_desktop_tokens(client, user["headers"])
        config = client.get(
            f"{API}/external/account-config",
            headers={"Authorization": f"Bearer {desktop['access_token']}"},
        )
        assert config.status_code == 200, config.text
        minted_rows = [
            provider
            for provider in config.json()["providers"]
            if provider["api_key"].startswith("sk-proj-minted-")
        ]
        assert len(minted_rows) == 1, minted_rows
        assert minted_rows[0]["api_key"] == expected_secret, (
            f"{user['email']} holds {minted_rows[0]['api_key']} but the server "
            f"recorded {ref} for them — a key is on the wrong account."
        )
        assert minted_rows[0]["is_admin_managed"] is True
        seen_secrets.add(minted_rows[0]["api_key"])

    assert seen_secrets == {"sk-proj-minted-1", "sk-proj-minted-2"}, (
        f"the two members share a key or hold the same one twice: {seen_secrets}"
    )


# ── The failure path is claimed too ─────────────────────────────────────────


def test_a_mint_that_fails_after_a_deactivation_leaves_the_row_suspended(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The *failure* write is conditional on the claim, not only the success one.

    The three claim tests above all drive a mint that succeeds. This drives one
    that fails while the same race is running, which is the branch with the
    sharper consequence: ``_record_failure`` writes ``pending`` with a retry
    time, so an unconditional version takes a row a deactivation has just
    settled ``suspended`` — a status whose documented meaning is "this account
    is disabled and nothing is being minted for it" — and hands it straight back
    to the converge queue.

    Reverting ``_record_failure`` to a plain ORM write turns this red and
    nothing else in the suite notices.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    def deactivate() -> None:
        response = client.patch(
            f"{API}/users/{user['id']}",
            headers=superuser_token_headers,
            json={"is_active": False},
        )
        assert response.status_code == 200, response.text

    with stub_minting_providers(
        on_mint=deactivate, mint_error="provider_error"
    ) as (_probes, provisioning):
        report = converge_keys(db)
        drain_tasks()

    # Nothing was created, so there is nothing to revoke — the interesting part
    # is the row, not the provider.
    assert provisioning.mint_count == 0
    assert provisioning.revoked == []
    assert "claim_lost" in report.skipped, report

    member = _member(client, superuser_token_headers, parent_id, str(user["id"]))
    assert member is not None
    assert member["provisioning_status"] == "suspended", member
    # And it stays out of the queue: a converge pass over a suspended row is not
    # an attempt, so nothing here should have moved.
    with stub_minting_providers() as (_probes, second):
        converge_keys(db)
    assert second.mint_count == 0


# ── Deactivation that fails part-way ────────────────────────────────────────


def test_a_deactivation_that_fails_part_way_still_revokes_what_it_took(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The keys already taken off their rows are still destroyed.

    ``suspend_user_memberships`` commits per member and erases
    ``external_key_ref`` as it goes, and ``on_account_deactivated`` is a
    deliberate never-fail net. Together they used to lose the collected
    revocations whenever the net fired: earlier members were committed with
    their refs erased and their revocation requests discarded, so their keys
    were live at the provider with nothing left anywhere naming them. That is
    the one outcome the whole file exists to prevent, and it was the never-fail
    net that produced it.

    The fix is that the sink belongs to the caller, so a partial result survives
    the failure. This test fails the *second* member's row write and asserts the
    first member's key was still destroyed.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    # Two minted records, so one deactivation walks two membership rows.
    for _ in range(2):
        _minted_parent(
            client,
            superuser_token_headers,
            admin_credential_id=admin_credential_id,
            target_user_ids=[str(user["id"])],
        )

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        provisioning.assert_minted(2)

        with failing_sql_statement(
            db,
            when_statement_contains="UPDATE managed_ai_credential_membership",
            occurrence=2,
        ) as injection:
            response = client.patch(
                f"{API}/users/{user['id']}",
                headers=superuser_token_headers,
                json={"is_active": False},
            )
            assert response.status_code == 200, response.text
        assert injection["fired"], (
            "the injection never fired; this test proves nothing"
        )
        drain_tasks()

    assert len(provisioning.revoked) == 1, (
        f"the deactivation collected a revocation, then failed, and discarded "
        f"it: {provisioning.revoked}"
    )


# ── The revoke that never got scheduled ─────────────────────────────────────


def test_a_revocation_that_cannot_be_scheduled_is_still_recorded(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``on_drop`` is the durable record, and nothing reached it before.

    ``schedule_revocations`` passes ``on_drop=`` precisely because the helper's
    historical answer to a failed cross-thread hand-off is a warning and
    ``coro.close()`` — the batch simply does not happen, and neither the log line
    nor anything else names the keys. Every existing test drains a *collected*
    coroutine, which is the other branch, so deleting the ``on_drop=`` argument
    and the whole of ``_record_unscheduled_revocations`` left the suite green
    while the record it exists to write stopped being written.

    ``dropped_background_tasks`` reproduces the drop faithfully (close, then
    call ``on_drop``). What must survive it is the ``revoke_failed`` event
    carrying the external ref: the membership row is gone by then, so that event
    is the only thing left that can name a key still live at the provider.
    """
    admin_credential_id = _admin_credential(client, superuser_token_headers)
    user = _new_user(client)
    parent = _minted_parent(
        client,
        superuser_token_headers,
        admin_credential_id=admin_credential_id,
        target_user_ids=[str(user["id"])],
    )
    parent_id = parent["record"]["id"]

    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        provisioning.assert_minted(1)

        with dropped_background_tasks(BACKGROUND_TASK_TARGETS_FULL) as dropped:
            removed = client.patch(
                f"{MANAGED_BASE}/{parent_id}",
                headers=superuser_token_headers,
                json={"target_user_ids": []},
            )
            assert removed.status_code == 200, removed.text
        assert "revoke_minted_keys" in dropped, dropped

    # Nothing reached the provider — that is what a drop means.
    assert provisioning.revoked == []

    events = client.get(
        f"{API}/security-events/",
        headers=user["headers"],
        params={"event_type": "admin.ai_credential.revoke_failed"},
    ).json()["data"]
    assert len(events) == 1, events
    details = events[0]["details"]
    assert details["reason"] == "not_scheduled", details
    assert details["external_key_ref"]["service_account_id"] == "svc_1", details
    assert "sk-proj-minted" not in str(details)
