"""Provider administration secrets, and the provider descriptions beside them.

The secret this surface stores can create and destroy API keys for a whole
provider organisation, so the tests that matter most here are the ones that
assert where it *is not*: not in any response body, and not in any of the four
generic paths that read every AI credential the platform holds.

That isolation is claimed to be structural — a different table, unreachable from
every foreign key those paths follow — but "structural" is exactly the kind of
claim that stops being true one refactor after it is written down, and the
refactor that would break it ("why is this a separate table? let's simplify") is
plausible. So it is asserted here rather than argued.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.desktop_auth import obtain_desktop_tokens
from tests.utils.utils import random_lower_string

# Pure CRUD over one table plus one read-only registry projection — no agents,
# no environments, and no default credentials needed.
NEEDS_AGENT_STUBS = False
NEEDS_DEFAULT_CREDENTIALS = False

API = settings.API_V1_STR
ADMIN_BASE = f"{API}/admin/provider-admin-credentials"
ADAPTERS_BASE = f"{API}/admin/provider-adapters"

SECRET = "sk-admin-not-a-real-secret"


def create_admin_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    provider_type: str = "openai",
    secret: str = SECRET,
    project_id: str = "proj_test",
    spend_limit_cents: int = 5000,
    expected_status: int = 200,
) -> dict:
    response = client.post(
        f"{ADMIN_BASE}/",
        headers=headers,
        json={
            "name": name or f"Org {random_lower_string()[:8]}",
            "provider_type": provider_type,
            "secret": secret,
            "config": {
                "project_id": project_id,
                "spend_limit_cents": spend_limit_cents,
            },
        },
    )
    assert response.status_code == expected_status, response.text
    return response.json()


# ── The secret never comes back ─────────────────────────────────────────────


def test_no_response_body_ever_carries_the_secret(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Asserted on the raw body's key set, not on a parsed model.

    A parsed model cannot see a field the schema does not declare, so parsing
    first would make this test blind to precisely the mistake it is looking for:
    an extra key in the JSON that the response model never mentioned.
    """
    created = create_admin_credential(client, superuser_token_headers)
    record_id = created["id"]

    bodies = [created]
    bodies.append(
        client.get(f"{ADMIN_BASE}/{record_id}", headers=superuser_token_headers).json()
    )
    listing = client.get(f"{ADMIN_BASE}/", headers=superuser_token_headers).json()
    bodies.extend(listing)
    bodies.append(
        client.patch(
            f"{ADMIN_BASE}/{record_id}",
            headers=superuser_token_headers,
            json={"name": "Renamed"},
        ).json()
    )

    for body in bodies:
        assert "secret" not in body, body.keys()
        assert "encrypted_secret" not in body, body.keys()
        assert SECRET not in str(body)
        assert body["has_secret"] is True


def test_only_superusers_can_reach_any_of_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    created = create_admin_credential(client, superuser_token_headers)
    for method, url in (
        ("get", f"{ADMIN_BASE}/"),
        ("get", f"{ADMIN_BASE}/{created['id']}"),
        ("get", f"{ADAPTERS_BASE}/"),
    ):
        response = getattr(client, method)(url, headers=normal_user_token_headers)
        assert response.status_code == 403, (url, response.text)

    assert (
        client.post(
            f"{ADMIN_BASE}/",
            headers=normal_user_token_headers,
            json={
                "name": "nope",
                "provider_type": "openai",
                "secret": SECRET,
                "config": {"project_id": "p", "spend_limit_cents": 100},
            },
        ).status_code
        == 403
    )


# ── The isolation invariant, made executable ────────────────────────────────


def test_the_admin_secret_is_invisible_to_every_generic_credential_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """The record appears in none of the four surfaces that read AI credentials.

    Each of these is correct for the table it reads, and each is a reason the
    admin secret must not be in that table: the user's own credential list and
    the managed-credential fleet table are projections, and
    ``/external/account-config`` hands every decrypted key a user owns to the
    desktop client.
    """
    created = create_admin_credential(client, superuser_token_headers)
    record_id = created["id"]
    # A real user credential, so the surfaces below are not trivially empty —
    # an assertion that nothing is listed proves nothing when nothing is listed.
    own = create_random_ai_credential(client, normal_user_token_headers)

    own_list = client.get(
        f"{API}/ai-credentials/", headers=normal_user_token_headers
    ).json()
    assert own["id"] in {row["id"] for row in own_list["data"]}
    assert record_id not in {row["id"] for row in own_list["data"]}
    assert SECRET not in str(own_list)

    admin_list = client.get(
        f"{API}/ai-credentials/", headers=superuser_token_headers
    ).json()
    assert record_id not in {row["id"] for row in admin_list["data"]}

    # The account-config bundle is native-client gated, so reaching it needs a
    # desktop token. Worth the setup: this is the endpoint that hands decrypted
    # keys to a program on someone's laptop, which is the sharpest reason the
    # admin secret lives in another table.
    desktop = obtain_desktop_tokens(client, normal_user_token_headers)
    account_config = client.get(
        f"{API}/external/account-config",
        headers={"Authorization": f"Bearer {desktop['access_token']}"},
    )
    assert account_config.status_code == 200, account_config.text
    assert record_id not in str(account_config.json())
    assert SECRET not in str(account_config.json())

    managed = client.get(
        f"{API}/admin/llm-providers/", headers=superuser_token_headers
    ).json()
    assert record_id not in {row["id"] for row in managed}


def test_no_endpoint_in_the_module_can_ever_return_the_secret() -> None:
    """Enumerated over the router, not over a list somebody maintains.

    The test above walks four named surfaces and the body test above that walks
    four named responses. Both are worth having and neither is structural: they
    are as complete as the day they were written, and a *fifth* endpoint added
    to this module is exactly the change that needs to fail here and would not.

    So this asserts over ``router.routes`` itself. Every route must declare a
    ``response_model``, and that model must be one of the projections known not
    to carry the secret — a new endpoint returning ``ProviderAdminCredential``
    (the table row, which holds ``encrypted_secret``) or declaring no model at
    all cannot be added without this failing and without somebody deciding, in
    writing, that the new shape is safe.
    """
    from fastapi.routing import APIRoute

    from app.api.routes.admin_provider_credentials import adapters_router, router
    from app.models.credentials.provider_admin_credential import (
        ProviderAdminCredentialPublic,
        ProviderAdminCredentialVerifyResult,
    )
    from app.models import Message
    from app.models.credentials.provider_adapter import ProviderAdaptersPublic

    # Every model a route here is permitted to return. Adding to this set is the
    # deliberate act the test exists to force.
    permitted = {
        ProviderAdminCredentialPublic,
        ProviderAdminCredentialVerifyResult,
        ProviderAdaptersPublic,
        Message,
    }

    problems: list[str] = []
    for source in (router, adapters_router):
        for route in source.routes:
            if not isinstance(route, APIRoute):
                continue
            model = route.response_model
            # ``list[X]`` — check the element type.
            origin_args = getattr(model, "__args__", None)
            candidates = list(origin_args) if origin_args else [model]
            for candidate in candidates:
                if candidate not in permitted:
                    problems.append(
                        f"{sorted(route.methods)} {route.path} returns "
                        f"{candidate!r}, which is not in the reviewed set of "
                        f"secret-free projections"
                    )
    assert not problems, "\n".join(problems)


def test_every_endpoint_in_the_module_declares_a_response_model() -> None:
    """A route with no ``response_model`` serialises whatever it returns.

    Separated from the test above because it is a different failure: that one
    catches a *wrong* declared shape, this one catches an *absent* one, where
    FastAPI falls back to serialising the returned object — a table row
    included, ``encrypted_secret`` and all.
    """
    from fastapi.routing import APIRoute

    from app.api.routes.admin_provider_credentials import adapters_router, router

    undeclared = [
        f"{sorted(route.methods)} {route.path}"
        for source in (router, adapters_router)
        for route in source.routes
        if isinstance(route, APIRoute) and route.response_model is None
    ]
    assert not undeclared, (
        f"routes with no response_model: {undeclared}. Declare one — an "
        f"undeclared route serialises whatever the handler returns."
    )


# ── Shape rules ─────────────────────────────────────────────────────────────


def test_a_provider_that_cannot_create_keys_gets_no_admin_secret(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Anthropic's administration API lists and updates keys; it cannot create.

    Storing an administration secret for it would offer an admin a capability
    that fails at the first mint — so it is refused at the point of connection,
    where the message can say why.
    """
    body = create_admin_credential(
        client,
        superuser_token_headers,
        provider_type="anthropic",
        expected_status=400,
    )
    assert "cannot create API keys" in str(body["detail"])


def test_replacing_the_secret_clears_the_verification_stamp(
    client: TestClient, superuser_token_headers: dict[str, str], db
) -> None:
    """A stamp from a different secret is a claim about a key nobody has tried."""
    from tests.utils.ai_provider import stub_minting_providers

    created = create_admin_credential(client, superuser_token_headers)
    record_id = created["id"]

    with stub_minting_providers() as (_probes, provisioning):
        verified = client.post(
            f"{ADMIN_BASE}/{record_id}/verify", headers=superuser_token_headers
        ).json()
    assert verified["ok"] is True
    assert verified["spend_limit_enforcing"] is True
    assert provisioning.calls, "the stubbed provider was never reached"

    stamped = client.get(
        f"{ADMIN_BASE}/{record_id}", headers=superuser_token_headers
    ).json()
    assert stamped["last_verified_at"] is not None

    rotated = client.patch(
        f"{ADMIN_BASE}/{record_id}",
        headers=superuser_token_headers,
        json={"secret": "sk-admin-rotated"},
    ).json()
    assert rotated["last_verified_at"] is None
    assert rotated["has_secret"] is True

    # Omitting the secret leaves the stored one alone — a rename must not
    # require an admin to re-paste an organisation key.
    renamed = client.patch(
        f"{ADMIN_BASE}/{record_id}",
        headers=superuser_token_headers,
        json={"name": "Renamed org"},
    ).json()
    assert renamed["name"] == "Renamed org"
    assert renamed["has_secret"] is True


def test_a_limit_that_is_not_enforced_is_not_a_cap(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The case a naive implementation passes.

    The provider returns a spend limit with a real ``threshold_amount`` and an
    enforcement status of ``inactive``. Anything checking the obvious field would
    call that capped. It is not, and Verify must say so.
    """
    from tests.utils.ai_provider import stub_minting_providers

    created = create_admin_credential(client, superuser_token_headers)

    with stub_minting_providers(
        enforcement_status="inactive", threshold_cents=5000
    ):
        result = client.post(
            f"{ADMIN_BASE}/{created['id']}/verify",
            headers=superuser_token_headers,
        ).json()

    assert result["ok"] is False
    assert result["error"] == "project_not_capped"
    assert result["spend_limit_enforcing"] is False
    # The threshold is reported precisely so the admin can see that the number
    # they set is there and doing nothing.
    assert result["spend_limit_cents"] == 5000


def test_a_project_with_no_limit_at_all_is_not_a_cap(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    from tests.utils.ai_provider import stub_minting_providers

    created = create_admin_credential(client, superuser_token_headers)

    with stub_minting_providers(spend_limit_absent=True):
        result = client.post(
            f"{ADMIN_BASE}/{created['id']}/verify",
            headers=superuser_token_headers,
        ).json()
    assert result["ok"] is False
    assert result["error"] == "project_not_capped"

    with stub_minting_providers(spend_limit_absent=True) as (_p, provisioning):
        applied = client.post(
            f"{ADMIN_BASE}/{created['id']}/apply-spend-limit",
            headers=superuser_token_headers,
        ).json()
    # Applying one sends integer cents on a monthly interval, and the value that
    # goes over the wire is the configured one — an off-by-100 here is a
    # hundred-fold cap.
    post = [
        call
        for call in provisioning.calls
        if call.method == "POST" and call.path.endswith("/spend_limit")
    ]
    assert len(post) == 1, provisioning.calls
    assert post[0].json_body == {
        "threshold_amount": 5000,
        "currency": "USD",
        "interval": "month",
    }
    assert applied["ok"] is True


def test_a_bad_secret_is_reported_and_stamped(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    from tests.utils.ai_provider import stub_minting_providers

    created = create_admin_credential(client, superuser_token_headers)
    with stub_minting_providers(project_missing=True):
        result = client.post(
            f"{ADMIN_BASE}/{created['id']}/verify",
            headers=superuser_token_headers,
        ).json()
    assert result["ok"] is False
    assert result["error"] == "project_not_found"

    stamped = client.get(
        f"{ADMIN_BASE}/{created['id']}", headers=superuser_token_headers
    ).json()
    assert stamped["last_verify_error"] == "project_not_found"
    assert stamped["last_verified_at"] is None


def test_deleting_an_unused_credential_is_allowed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    created = create_admin_credential(client, superuser_token_headers)
    response = client.delete(
        f"{ADMIN_BASE}/{created['id']}", headers=superuser_token_headers
    )
    assert response.status_code == 200, response.text
    assert (
        client.get(
            f"{ADMIN_BASE}/{created['id']}", headers=superuser_token_headers
        ).status_code
        == 404
    )


def test_a_missing_credential_is_a_404_not_a_500(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    ghost = uuid.uuid4()
    assert (
        client.get(f"{ADMIN_BASE}/{ghost}", headers=superuser_token_headers).status_code
        == 404
    )


# ── The provider registry projection ────────────────────────────────────────


def test_provider_adapters_covers_every_credential_type(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Derived from the enum, not from a literal list.

    A literal list is green on the day a sixth provider is added and silently
    missing from the endpoint that is supposed to be the one description of what
    the server supports.
    """
    from app.models.credentials.ai_credential import AICredentialType

    body = client.get(f"{ADAPTERS_BASE}/", headers=superuser_token_headers).json()
    described = {row["type"] for row in body["data"]}
    assert described == {t.value for t in AICredentialType}
    assert body["count"] == len(described)


def test_provider_adapters_says_which_provider_can_mint(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """One server-side answer, so no client keeps its own provider table."""
    body = client.get(f"{ADAPTERS_BASE}/", headers=superuser_token_headers).json()
    by_type = {row["type"]: row for row in body["data"]}

    assert by_type["openai"]["supports_minting"] is True
    assert by_type["openai"]["admin_config_schema"]["fields"]
    assert by_type["anthropic"]["supports_minting"] is False
    assert by_type["anthropic"]["admin_config_schema"] is None
    # The facts the browser currently keeps three disagreeing copies of.
    assert by_type["openai_compatible"]["requires_base_url"] is True
    assert by_type["openai_compatible"]["requires_model"] is True
    assert by_type["anthropic"]["issues_oauth_tokens"] is True
    assert by_type["anthropic"]["label"]


def test_changing_the_project_clears_the_verification_stamp(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A verification is of a (secret, project) pair, not of a secret.

    Pointing the record at a different project after verifying leaves a stamp
    that reports "verified, capped" about a project nobody has checked. The two
    clears are one clear for that reason — an asymmetry here is a stamp that
    quietly means something different depending on which field was edited last.
    """
    from tests.utils.ai_provider import stub_minting_providers

    created = create_admin_credential(client, superuser_token_headers)
    record_id = created["id"]

    with stub_minting_providers():
        assert (
            client.post(
                f"{ADMIN_BASE}/{record_id}/verify", headers=superuser_token_headers
            ).json()["ok"]
            is True
        )
    assert client.get(
        f"{ADMIN_BASE}/{record_id}", headers=superuser_token_headers
    ).json()["last_verified_at"] is not None

    moved = client.patch(
        f"{ADMIN_BASE}/{record_id}",
        headers=superuser_token_headers,
        json={"config": {"project_id": "proj_other", "spend_limit_cents": 5000}},
    ).json()
    assert moved["config"]["project_id"] == "proj_other"
    assert moved["last_verified_at"] is None

    # A PATCH that changes nothing about the config keeps the stamp: the
    # verification is still of the pair it was made against.
    with stub_minting_providers():
        client.post(f"{ADMIN_BASE}/{record_id}/verify", headers=superuser_token_headers)
    unchanged = client.patch(
        f"{ADMIN_BASE}/{record_id}",
        headers=superuser_token_headers,
        json={"config": {"project_id": "proj_other", "spend_limit_cents": 5000}},
    ).json()
    assert unchanged["last_verified_at"] is not None


def test_the_server_answers_whether_delete_will_be_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``delete_blocked`` is the answer; the counts are the explanation.

    A client computing ``(a or b) > 0`` for itself would be re-deriving a server
    policy in the browser, and would keep answering the old way the day the rule
    changes.
    """
    created = create_admin_credential(client, superuser_token_headers)
    assert created["delete_blocked"] is False
    assert created["minting_credential_count"] == 0
    assert created["live_minted_key_count"] == 0

    minted = client.post(
        f"{settings.API_V1_STR}/admin/llm-providers/",
        headers=superuser_token_headers,
        json={
            "name": "Minted record",
            "type": "openai",
            "provisioning_mode": "minted",
            "provider_admin_credential_id": created["id"],
            "target_user_ids": [],
        },
    )
    assert minted.status_code == 200, minted.text

    after = client.get(
        f"{ADMIN_BASE}/{created['id']}", headers=superuser_token_headers
    ).json()
    assert after["delete_blocked"] is True
    assert after["minting_credential_count"] == 1
    # No member has a key yet, so nothing is stranded — but the configuration
    # would still be lost, which is why the two counts are separate.
    assert after["live_minted_key_count"] == 0
