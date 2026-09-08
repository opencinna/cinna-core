"""``/admin/ai-providers`` — the superuser surface over key sources.

A **provider** is where keys come from plus the rule for who automatically gets
one. Its secret means one of two categorically different things: a model API key
for a ``fixed_key`` provider, and an organisation administration secret — the
power to create and destroy keys for a whole provider organisation — for a
``minted`` one. So the tests that matter most here are still the ones that assert
where the secret *is not*: not in any response body, and not in any of the
generic paths that read every AI credential the platform holds.

That isolation is claimed to be structural — a different table, unreachable from
every foreign key those paths follow — but "structural" is exactly the kind of
claim that stops being true one refactor after it is written down, and the
refactor that would break it ("why is this a separate table? let's simplify") is
plausible. So it is asserted here rather than argued.

WHAT THIS FILE REPLACES
-----------------------
``provider_admin_credentials_test.py``, deleted with the route it covered
(``/admin/provider-admin-credentials`` and ``/admin/provider-adapters``, both
gone in Phase 4 of the ai-credential-providers plan). Everything in it that is
still true was ported here and retargeted; the three assertions that were about
fields ``AIProviderPublic`` does not carry (``delete_blocked``,
``minting_credential_count``, ``live_minted_key_count``) are replaced by the
409 delete gate, which is where that answer lives now.

The service-level half of this surface — policy write-through, incumbent-wins
grants, the conflict rule's internals — is
``ai_providers_service_test.py``. This file covers what the *route* adds: the
status codes, the error envelopes a dialog parses, and the auth boundary.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.ai_provider import stub_minting_providers
from tests.utils.desktop_auth import obtain_desktop_tokens
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_lower_string

# Admin CRUD over two tables plus one read-only registry projection — no
# agents, no environments, and no seeded default credential.
NEEDS_AGENT_STUBS = False
NEEDS_DEFAULT_CREDENTIALS = False

API = settings.API_V1_STR
PROVIDERS = f"{API}/admin/ai-providers"
MANAGED = f"{API}/admin/llm-providers"

#: An organisation administration secret — a ``minted`` provider's secret. It is
#: never handed to anybody: it creates keys, it is not one of them.
ADMIN_SECRET = "sk-admin-not-a-real-secret"
#: A ``fixed_key`` provider's secret — an ordinary model key, which members do
#: legitimately receive a copy of. It must still never come back from *this*
#: surface.
FIXED_SECRET = "sk-ant-not-a-real-fixed-key"


# ── Local helpers ───────────────────────────────────────────────────────────


def create_minted_provider(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    provider_type: str = "openai",
    secret: str = ADMIN_SECRET,
    project_id: str | None = "proj_test",
    expected_status: int = 200,
    **extra,
) -> dict:
    """A per-user-keys provider. Only OpenAI's adapter can mint."""
    config: dict = {}
    if project_id is not None:
        config["project_id"] = project_id
    response = client.post(
        f"{PROVIDERS}/",
        headers=headers,
        json={
            "name": name or f"Org {random_lower_string()[:8]}",
            "kind": "minted",
            "type": provider_type,
            "secret": secret,
            "config": config,
            **extra,
        },
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def create_fixed_key_provider(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    provider_type: str = "anthropic",
    secret: str = FIXED_SECRET,
    expected_status: int = 200,
    **extra,
) -> dict:
    """One pasted key, copied onto every member's child credential."""
    response = client.post(
        f"{PROVIDERS}/",
        headers=headers,
        json={
            "name": name or f"Company {random_lower_string()[:8]}",
            "kind": "fixed_key",
            "type": provider_type,
            "secret": secret,
            **extra,
        },
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def get_provider(
    client: TestClient, headers: dict[str, str], provider_id: str
) -> dict:
    response = client.get(f"{PROVIDERS}/{provider_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def list_adapters(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.get(f"{PROVIDERS}/adapters", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def own_credentials(client: TestClient, headers: dict[str, str]) -> list[dict]:
    response = client.get(f"{API}/ai-credentials/", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def desktop_access_token(client: TestClient, headers: dict[str, str]) -> str:
    """Run the consent dance once and keep the token.

    Once per user per test: the helper looks its registered client up by device
    name, so a second dance for the same name would exchange a code against the
    wrong registration.
    """
    return obtain_desktop_tokens(client, headers)["access_token"]


def account_config(client: TestClient, access_token: str) -> dict:
    """The decrypted bundle the desktop client receives, for one user."""
    response = client.get(
        f"{API}/external/account-config",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── The secret never comes back ─────────────────────────────────────────────


def test_no_response_body_ever_carries_the_secret(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Asserted on the raw body's key set, not on a parsed model.

    A parsed model cannot see a field the schema does not declare, so parsing
    first would make this test blind to precisely the mistake it is looking for:
    an extra key in the JSON that the response model never mentioned.

    Both kinds, because the two secrets are different things and only one of
    them is an organisation administration key. A ``fixed_key`` provider's key
    is copied onto members' credentials by design — that is not a reason for
    *this* surface to hand it back to a browser.
    """
    minted = create_minted_provider(client, superuser_token_headers)
    fixed = create_fixed_key_provider(client, superuser_token_headers)

    bodies = [minted, fixed]
    for record in (minted, fixed):
        bodies.append(get_provider(client, superuser_token_headers, record["id"]))
        bodies.append(
            client.patch(
                f"{PROVIDERS}/{record['id']}",
                headers=superuser_token_headers,
                json={"name": "Renamed"},
            ).json()
        )
    listing = client.get(f"{PROVIDERS}/", headers=superuser_token_headers)
    assert listing.status_code == 200, listing.text
    bodies.extend(listing.json())

    assert len(bodies) == 8
    for body in bodies:
        assert "secret" not in body, body.keys()
        assert "encrypted_secret" not in body, body.keys()
        assert ADMIN_SECRET not in str(body)
        assert FIXED_SECRET not in str(body)
        assert body["has_secret"] is True


def test_only_superusers_can_reach_any_of_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """Every route in the module, including the read-only adapters projection.

    ``/adapters`` describes what the server can be configured with, which is not
    a user-facing fact and is the endpoint most likely to be waved through as
    harmless.
    """
    created = create_fixed_key_provider(client, superuser_token_headers)
    provider_id = created["id"]

    for method, url in (
        ("get", f"{PROVIDERS}/"),
        ("get", f"{PROVIDERS}/{provider_id}"),
        ("get", f"{PROVIDERS}/adapters"),
        ("delete", f"{PROVIDERS}/{provider_id}"),
    ):
        response = getattr(client, method)(url, headers=normal_user_token_headers)
        assert response.status_code == 403, (url, response.text)

    for url, body in (
        (
            f"{PROVIDERS}/",
            {
                "name": "nope",
                "kind": "fixed_key",
                "type": "anthropic",
                "secret": FIXED_SECRET,
            },
        ),
        (f"{PROVIDERS}/{provider_id}/verify", {}),
        (f"{PROVIDERS}/{provider_id}/rotate-key", {"api_key": "sk-ant-other"}),
        (f"{PROVIDERS}/{provider_id}/apply-to-existing", {}),
    ):
        response = client.post(url, headers=normal_user_token_headers, json=body)
        assert response.status_code == 403, (url, response.text)

    patched = client.patch(
        f"{PROVIDERS}/{provider_id}",
        headers=normal_user_token_headers,
        json={"name": "nope"},
    )
    assert patched.status_code == 403, patched.text

    # The control: the provider is still there and still named what it was, so
    # the 403s above refused the work rather than merely the response.
    assert get_provider(client, superuser_token_headers, provider_id)["name"] == (
        created["name"]
    )


# ── The isolation invariant, made executable ────────────────────────────────


def test_the_admin_secret_is_invisible_to_every_generic_credential_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """The provider row appears in none of the surfaces that read AI credentials.

    Each of these is correct for the table it reads, and each is a reason the
    administration secret must not be in that table: the user's own credential
    list and the managed-credential fleet table are projections, and
    ``/external/account-config`` hands every decrypted key a user owns to the
    desktop client.
    """
    created = create_minted_provider(client, superuser_token_headers)
    provider_id = created["id"]
    # A real user credential, so the surfaces below are not trivially empty —
    # an assertion that nothing is listed proves nothing when nothing is listed.
    own = create_random_ai_credential(client, normal_user_token_headers)

    own_list = own_credentials(client, normal_user_token_headers)
    assert own["id"] in {row["id"] for row in own_list}
    assert provider_id not in {row["id"] for row in own_list}
    assert ADMIN_SECRET not in str(own_list)

    admin_list = own_credentials(client, superuser_token_headers)
    assert provider_id not in {row["id"] for row in admin_list}
    assert ADMIN_SECRET not in str(admin_list)

    # The account-config bundle is native-client gated, so reaching it needs a
    # desktop token. Worth the setup: this is the endpoint that hands decrypted
    # keys to a program on someone's laptop, which is the sharpest reason the
    # administration secret lives in another table.
    bundle = account_config(
        client, desktop_access_token(client, normal_user_token_headers)
    )
    assert own["id"] in {row["credential_id"] for row in bundle["providers"]}
    assert provider_id not in str(bundle)
    assert ADMIN_SECRET not in str(bundle)

    managed = client.get(f"{MANAGED}/", headers=superuser_token_headers)
    assert managed.status_code == 200, managed.text
    rows = managed.json()
    # The credential the provider owns *is* listed there — that is the record
    # the fleet table edits. The provider, and its secret, are not.
    assert created["owned_credential_id"] in {row["id"] for row in rows}
    assert provider_id not in {row["id"] for row in rows}
    assert ADMIN_SECRET not in str(rows)


def test_no_endpoint_in_the_module_can_ever_return_the_secret() -> None:
    """Enumerated over the router, not over a list somebody maintains.

    The test above walks four named surfaces and the body test above that walks
    eight named responses. Both are worth having and neither is structural: they
    are as complete as the day they were written, and a *tenth* endpoint added
    to this module is exactly the change that needs to fail here and would not.

    So this asserts over ``router.routes`` itself. Every route must declare a
    ``response_model``, and that model must be one of the projections known not
    to carry the secret — a new endpoint returning ``AIProvider`` (the table
    row, which holds ``encrypted_secret``) or declaring no model at all cannot
    be added without this failing and without somebody deciding, in writing,
    that the new shape is safe.
    """
    from fastapi.routing import APIRoute

    from app.api.routes import admin_ai_providers
    from app.models import Message
    from app.models.credentials.managed_ai_credential import (
        ManagedAICredentialApplyResult,
    )
    from app.models.credentials.provider_adapter import ProviderAdaptersPublic
    from app.models.credentials.provider_admin_credential import (
        AIProviderPublic,
        AIProviderVerifyResult,
    )

    # Every model a route here is permitted to return. Adding to this set is the
    # deliberate act the test exists to force.
    permitted = {
        AIProviderPublic,
        AIProviderVerifyResult,
        ManagedAICredentialApplyResult,
        ProviderAdaptersPublic,
        Message,
    }

    problems: list[str] = []
    routes = [
        route
        for route in admin_ai_providers.router.routes
        if isinstance(route, APIRoute)
    ]
    # The nine endpoints of §5.7. A route that disappears is as much a drift as
    # one that appears, and without this the loop below is vacuously green on an
    # empty router.
    assert len(routes) == 9, sorted(
        f"{sorted(route.methods)} {route.path}" for route in routes
    )
    for route in routes:
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
    included, ``encrypted_secret`` and all. (A ``-> Model`` return annotation
    counts: FastAPI derives the response model from it, which is how the
    ``DELETE`` route declares ``Message``.)
    """
    from fastapi.routing import APIRoute

    from app.api.routes import admin_ai_providers

    undeclared = [
        f"{sorted(route.methods)} {route.path}"
        for route in admin_ai_providers.router.routes
        if isinstance(route, APIRoute) and route.response_model is None
    ]
    assert not undeclared, (
        f"routes with no response_model: {undeclared}. Declare one — an "
        f"undeclared route serialises whatever the handler returns."
    )


# ── Shape rules ─────────────────────────────────────────────────────────────


def test_per_user_keys_are_refused_for_a_provider_that_cannot_create_them(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Anthropic's administration API lists and updates keys; it cannot create.

    Offering per-user keys for it would hand an admin a configuration that fails
    at the first mint — so it is refused at the point of connection, where the
    message can say why and name the alternative.
    """
    body = create_minted_provider(
        client,
        superuser_token_headers,
        provider_type="anthropic",
        expected_status=400,
    )
    detail = str(body["detail"])
    assert "cannot create API keys" in detail
    assert "fixed key" in detail


def test_per_user_keys_without_a_project_are_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """There is no project to mint into, and so no spend limit to verify.

    The cap is what bounds every key the provider creates, and it is a property
    of the project — so a provider with no project id cannot be minted through
    at all, and is refused rather than stored and discovered later.
    """
    body = create_minted_provider(
        client,
        superuser_token_headers,
        project_id=None,
        expected_status=400,
    )
    assert "project id" in str(body["detail"])

    # The control: the same request with a project id is accepted, so the 400
    # above is about the missing project and not about the shape of the call.
    accepted = create_minted_provider(client, superuser_token_headers)
    assert accepted["config"]["project_id"] == "proj_test"


def test_verifying_a_minted_provider_stamps_what_it_learned(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Verify answers two questions and records both outcomes.

      1. A good secret over a capped project stamps ``last_verified_at``
      2. A secret the provider does not recognise stamps ``last_verify_error``
         and leaves ``last_verified_at`` unset — a stamp from a failed check
         would report "verified" about a configuration nobody has confirmed
    """
    created = create_minted_provider(client, superuser_token_headers)
    provider_id = created["id"]
    assert created["last_verified_at"] is None

    with stub_minting_providers() as (_probes, provisioning):
        verified = client.post(
            f"{PROVIDERS}/{provider_id}/verify", headers=superuser_token_headers
        )
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["ok"] is True
    assert body["checked_spend_limit"] is True
    assert body["spend_limit_enforcing"] is True
    assert body["error"] is None
    assert provisioning.calls, "the stubbed provider was never reached"

    stamped = get_provider(client, superuser_token_headers, provider_id)
    assert stamped["last_verified_at"] is not None
    assert stamped["last_verify_error"] is None

    with stub_minting_providers(project_missing=True):
        failed = client.post(
            f"{PROVIDERS}/{provider_id}/verify", headers=superuser_token_headers
        ).json()
    assert failed["ok"] is False
    assert failed["error"] == "project_not_found"

    restamped = get_provider(client, superuser_token_headers, provider_id)
    assert restamped["last_verify_error"] == "project_not_found"


def test_a_bad_secret_is_reported_and_stamped_on_a_fresh_provider(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``last_verified_at`` stays NULL when the first check is the failed one.

    The test above re-verifies a provider that had already succeeded, so its
    ``last_verified_at`` is legitimately non-null afterwards. This is the case
    that pins the *absence*: a failure must not stamp a success.
    """
    created = create_minted_provider(client, superuser_token_headers)

    with stub_minting_providers(project_missing=True):
        result = client.post(
            f"{PROVIDERS}/{created['id']}/verify",
            headers=superuser_token_headers,
        ).json()
    assert result["ok"] is False
    assert result["error"] == "project_not_found"

    stamped = get_provider(client, superuser_token_headers, created["id"])
    assert stamped["last_verify_error"] == "project_not_found"
    assert stamped["last_verified_at"] is None


def test_changing_the_project_clears_the_verification_stamp(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A verification is of a (secret, project) pair, not of a secret.

    Pointing the provider at a different project after verifying leaves a stamp
    that reports "verified, capped" about a project nobody has checked.
    """
    created = create_minted_provider(client, superuser_token_headers)
    provider_id = created["id"]

    with stub_minting_providers():
        assert (
            client.post(
                f"{PROVIDERS}/{provider_id}/verify", headers=superuser_token_headers
            ).json()["ok"]
            is True
        )
    assert (
        get_provider(client, superuser_token_headers, provider_id)["last_verified_at"]
        is not None
    )

    moved = client.patch(
        f"{PROVIDERS}/{provider_id}",
        headers=superuser_token_headers,
        json={"config": {"project_id": "proj_other"}},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["config"]["project_id"] == "proj_other"
    assert moved.json()["last_verified_at"] is None

    # A PATCH that changes nothing about the config keeps the stamp: the
    # verification is still of the pair it was made against.
    with stub_minting_providers():
        client.post(
            f"{PROVIDERS}/{provider_id}/verify", headers=superuser_token_headers
        )
    unchanged = client.patch(
        f"{PROVIDERS}/{provider_id}",
        headers=superuser_token_headers,
        json={"config": {"project_id": "proj_other"}},
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["last_verified_at"] is not None

    # A rename leaves the stamp alone too — re-pasting an organisation key to
    # rename a provider is not something an admin should have to do.
    renamed = client.patch(
        f"{PROVIDERS}/{provider_id}",
        headers=superuser_token_headers,
        json={"name": "Renamed org"},
    ).json()
    assert renamed["name"] == "Renamed org"
    assert renamed["has_secret"] is True
    assert renamed["last_verified_at"] is not None


def test_rotating_a_fixed_key_providers_key_clears_the_verification_stamp(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A stamp from a different key is a claim about a key nobody has tried."""
    created = create_fixed_key_provider(client, superuser_token_headers)
    provider_id = created["id"]

    with stub_minting_providers() as (probes, _provisioning):
        verified = client.post(
            f"{PROVIDERS}/{provider_id}/verify", headers=superuser_token_headers
        ).json()
    assert verified["ok"] is True
    # A fixed key has no project, so the spend fields are not answers here.
    assert verified["checked_spend_limit"] is False
    assert probes.calls, "the stubbed provider was never probed"
    assert (
        get_provider(client, superuser_token_headers, provider_id)["last_verified_at"]
        is not None
    )

    rotated = client.post(
        f"{PROVIDERS}/{provider_id}/rotate-key",
        headers=superuser_token_headers,
        json={"api_key": "sk-ant-rotated-not-real"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["last_verified_at"] is None
    assert rotated.json()["has_secret"] is True


# ── Spend-limit semantics ───────────────────────────────────────────────────


def test_a_capped_project_that_has_not_hit_its_limit_is_a_cap(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The case the first implementation got backwards, and the only live one.

    ``enforcement.status`` is the hard limit's **current** runtime state, not its
    configuration — the provider's own generated types say "Whether the hard
    spend limit is *currently* enforcing". A project capped at $50 and sitting at
    $0 of it therefore reports ``inactive``, which is the state every healthy
    capped project is in essentially all of the time. ``enforcing`` means the
    threshold has been reached and the project is already refusing traffic.

    The predicate used to be ``status == "enforcing"``, which refused every
    healthy project and would have admitted only an exhausted one — where a
    minted key is dead on arrival. It could not pass in the state it was written
    to allow. This is that regression.
    """
    created = create_minted_provider(client, superuser_token_headers)

    with stub_minting_providers(enforcement_status="inactive", threshold_cents=5000):
        result = client.post(
            f"{PROVIDERS}/{created['id']}/verify", headers=superuser_token_headers
        ).json()

    assert result["ok"] is True
    assert result["error"] is None
    assert result["spend_limit_enforcing"] is True
    assert result["spend_limit_cents"] == 5000


def test_a_project_already_over_its_limit_is_still_a_cap(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``enforcing`` is a cap too — it is the same limit, currently biting.

    Kept as its own case so neither runtime state can be quietly excluded by a
    predicate that reaches for the status field again.
    """
    created = create_minted_provider(client, superuser_token_headers)

    with stub_minting_providers(enforcement_status="enforcing", threshold_cents=5000):
        result = client.post(
            f"{PROVIDERS}/{created['id']}/verify", headers=superuser_token_headers
        ).json()

    assert result["ok"] is True
    assert result["spend_limit_enforcing"] is True


def test_a_project_with_no_limit_at_all_is_not_a_cap(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    created = create_minted_provider(client, superuser_token_headers)

    with stub_minting_providers(spend_limit_absent=True):
        result = client.post(
            f"{PROVIDERS}/{created['id']}/verify", headers=superuser_token_headers
        ).json()
    assert result["ok"] is False
    assert result["error"] == "project_not_capped"


def test_cinna_never_writes_a_spend_limit(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The limit is the provider console's to own, and only ever read here.

    There is no endpoint that sets one and no configured threshold to send, so
    the strongest available statement is about the wire: across a verify of an
    uncapped project — the exact situation that used to invite an "Apply spend
    limit" press — not one write reaches the provider's spend-limit endpoint.

    A locally stored threshold was removed rather than merely hidden. The same
    organisation is reachable from the console and from any other tool pointed at
    it, so a copy on this side goes stale the moment somebody edits the real one,
    and a stale number displayed as the project's cap is worse than none.
    """
    created = create_minted_provider(client, superuser_token_headers)

    with stub_minting_providers(spend_limit_absent=True) as (_p, provisioning):
        client.post(
            f"{PROVIDERS}/{created['id']}/verify", headers=superuser_token_headers
        )

    writes = [
        call
        for call in provisioning.calls
        if call.path.endswith("/spend_limit") and call.method != "GET"
    ]
    assert writes == [], writes
    assert any(
        call.method == "GET" and call.path.endswith("/spend_limit")
        for call in provisioning.calls
    ), "the cap was never read at all"


# ── Not-found ───────────────────────────────────────────────────────────────


def test_a_missing_provider_is_a_404_not_a_500(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Every id-addressed route answers the same way for an id nobody has."""
    ghost = uuid.uuid4()

    assert (
        client.get(f"{PROVIDERS}/{ghost}", headers=superuser_token_headers).status_code
        == 404
    )
    assert (
        client.patch(
            f"{PROVIDERS}/{ghost}",
            headers=superuser_token_headers,
            json={"name": "ghost"},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"{PROVIDERS}/{ghost}", headers=superuser_token_headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{PROVIDERS}/{ghost}/verify", headers=superuser_token_headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{PROVIDERS}/{ghost}/rotate-key",
            headers=superuser_token_headers,
            json={"api_key": "sk-ant-nope"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{PROVIDERS}/{ghost}/apply-to-existing",
            headers=superuser_token_headers,
        ).status_code
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

    body = list_adapters(client, superuser_token_headers)
    described = {row["type"] for row in body["data"]}
    assert described == {t.value for t in AICredentialType}
    assert body["count"] == len(described)


def test_provider_adapters_says_which_provider_can_mint(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """One server-side answer, so no client keeps its own provider table.

    ``supports_minting`` is the whole answer, and it is a fact about the
    adapter: a provider carries its own administration secret, so minting has
    no precondition beyond the adapter being able to do it. That is asserted
    **before and after** two providers are connected, because the field it
    replaced — ``can_mint_now``, ``supports_minting and type in
    connected_types`` — differed between exactly those two moments, and
    answered ``False`` for the first OpenAI provider an admin ever connects,
    which is the case the create wizard exists to serve.

    The enforcing rule reads the same single term
    (``AIProvidersService._validate_shape``); the create-time 400 for a
    ``minted`` Anthropic provider is pinned by
    ``test_per_user_keys_are_refused_for_a_provider_that_cannot_create_them``.
    """
    body = list_adapters(client, superuser_token_headers)
    by_type = {row["type"]: row for row in body["data"]}

    assert by_type["openai"]["supports_minting"] is True
    assert by_type["openai"]["admin_config_schema"]["fields"]
    assert by_type["anthropic"]["supports_minting"] is False
    assert by_type["anthropic"]["admin_config_schema"] is None
    # The facts the browser used to keep three disagreeing copies of.
    assert by_type["openai_compatible"]["requires_base_url"] is True
    assert by_type["openai_compatible"]["requires_model"] is True
    assert by_type["anthropic"]["issues_oauth_tokens"] is True
    assert by_type["anthropic"]["label"]

    # No field on this projection is a function of what is connected.
    create_fixed_key_provider(client, superuser_token_headers)
    create_minted_provider(client, superuser_token_headers)
    after = {
        row["type"]: row
        for row in list_adapters(client, superuser_token_headers)["data"]
    }
    assert after == by_type

    # And the retired conjunction is not on the wire under any name.
    assert "can_mint_now" not in by_type["openai"]


# ── The delete gate (§5.4) ──────────────────────────────────────────────────


def test_deleting_a_provider_people_hold_keys_from_is_refused_then_forced(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The gate, and what force costs:
      1. A fixed-key provider is created with two members, who now hold a
         child credential each
      2. An unforced DELETE is refused with 409 carrying the impact — named
         people, because "2 users will lose a key" is not something an
         administrator can check before pressing
      3. Nothing was deleted by the refusal
      4. ``force=true`` succeeds
      5. The provider and the managed credential it owned are both gone
      6. Both members' child credentials are gone from their own listing
    """
    first, first_headers = create_random_user_with_headers(client)
    second, second_headers = create_random_user_with_headers(client)

    # ── Phase 1 ─────────────────────────────────────────────────────────
    provider = create_fixed_key_provider(
        client,
        superuser_token_headers,
        name="Company Claude",
        target_user_ids=[first["id"], second["id"]],
    )
    provider_id = provider["id"]
    credential_id = provider["owned_credential_id"]
    assert provider["member_count"] == 2

    held = {
        person["id"]: {
            row["id"]
            for row in own_credentials(client, headers)
            if row["is_admin_managed"]
        }
        for person, headers in ((first, first_headers), (second, second_headers))
    }
    assert all(len(ids) == 1 for ids in held.values()), held

    # ── Phase 2: refused, with the impact ───────────────────────────────
    refused = client.delete(
        f"{PROVIDERS}/{provider_id}", headers=superuser_token_headers
    )
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "ai_provider_in_use"
    impact = detail["impact"]
    assert impact["provider_id"] == provider_id
    assert impact["provider_name"] == "Company Claude"
    assert impact["owned_credential_id"] == credential_id
    assert impact["member_count"] == 2
    # A fixed key is not minted at the provider, so nothing is revoked there.
    assert impact["minted_key_count"] == 0
    assert {member["email"] for member in impact["members"]} == {
        first["email"],
        second["email"],
    }
    assert all(
        member["holds_provider_key"] is False for member in impact["members"]
    )

    # ── Phase 3: the refusal changed nothing ────────────────────────────
    assert get_provider(client, superuser_token_headers, provider_id)["member_count"] == 2

    # ── Phase 4: forced ─────────────────────────────────────────────────
    forced = client.delete(
        f"{PROVIDERS}/{provider_id}?force=true", headers=superuser_token_headers
    )
    assert forced.status_code == 200, forced.text

    # ── Phase 5: provider and managed credential both gone ──────────────
    assert (
        client.get(
            f"{PROVIDERS}/{provider_id}", headers=superuser_token_headers
        ).status_code
        == 404
    )
    managed = client.get(f"{MANAGED}/", headers=superuser_token_headers)
    assert managed.status_code == 200, managed.text
    assert credential_id not in {row["id"] for row in managed.json()}

    # ── Phase 6: the members' own credentials went with it ──────────────
    for person, headers in ((first, first_headers), (second, second_headers)):
        remaining = {row["id"] for row in own_credentials(client, headers)}
        assert not (held[person["id"]] & remaining), remaining


def test_a_provider_nobody_holds_a_key_from_deletes_without_a_confirmation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """There is nobody to warn, so there is nothing to confirm."""
    provider = create_fixed_key_provider(client, superuser_token_headers)
    assert provider["member_count"] == 0

    response = client.delete(
        f"{PROVIDERS}/{provider['id']}", headers=superuser_token_headers
    )
    assert response.status_code == 200, response.text
    assert (
        client.get(
            f"{PROVIDERS}/{provider['id']}", headers=superuser_token_headers
        ).status_code
        == 404
    )
    assert provider["owned_credential_id"] not in {
        row["id"]
        for row in client.get(f"{MANAGED}/", headers=superuser_token_headers).json()
    }


# ── Rotation ────────────────────────────────────────────────────────────────


def test_rotating_a_fixed_key_provider_re_keys_every_member(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The new key reaches the member's own credential, not just the provider.

    Asserted through ``/external/account-config``, which is the one endpoint
    that returns a member's key decrypted — so "re-keyed" means the value the
    desktop client would receive changed, rather than a row's timestamp moving.
    """
    member, member_headers = create_random_user_with_headers(client)
    provider = create_fixed_key_provider(
        client, superuser_token_headers, target_user_ids=[member["id"]]
    )

    token = desktop_access_token(client, member_headers)
    before = account_config(client, token)["providers"]
    assert FIXED_SECRET in {row["api_key"] for row in before}

    rotated = client.post(
        f"{PROVIDERS}/{provider['id']}/rotate-key",
        headers=superuser_token_headers,
        json={"api_key": "sk-ant-rotated-not-real"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["has_secret"] is True

    after = account_config(client, token)["providers"]
    keys = {row["api_key"] for row in after}
    assert "sk-ant-rotated-not-real" in keys
    assert FIXED_SECRET not in keys


def test_rotating_a_minted_provider_is_refused_rather_than_ignored(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """400, and the message says there is nothing here to rotate.

    That provider's secret creates keys, it is not one of them. Accepting the
    request would store a key nothing reads and leave the admin believing they
    had rolled one — which is the entire point of refusing.
    """
    provider = create_minted_provider(client, superuser_token_headers)

    response = client.post(
        f"{PROVIDERS}/{provider['id']}/rotate-key",
        headers=superuser_token_headers,
        json={"api_key": "sk-admin-rotated-not-real"},
    )
    assert response.status_code == 400, response.text
    assert "nothing to rotate" in str(response.json()["detail"])

    # And the refusal is a refusal rather than a 400 thrown after the write:
    # the provider is unchanged and its stored secret still authenticates.
    after = get_provider(client, superuser_token_headers, provider["id"])
    assert after["updated_at"] == provider["updated_at"]
    with stub_minting_providers() as (_probes, provisioning):
        assert (
            client.post(
                f"{PROVIDERS}/{provider['id']}/verify",
                headers=superuser_token_headers,
            ).json()["ok"]
            is True
        )
    assert provisioning.calls, "the stubbed provider was never reached"


# ── The default-slot conflict, as an envelope a dialog parses ───────────────


def test_two_providers_cannot_claim_the_same_default_slot(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``User.default_ai_credential_<mode>_id`` holds exactly one credential, so
    two providers wiring it for the same role would fight over it silently, per
    user, in row order. The second configuration is refused at write time:

      1. "Company OpenAI" wires both modes for developers
      2. A second provider claiming ``building`` for developers → 409, and the
         envelope names the field the dialog highlights
      3. Nothing was written — a refused create leaves no half-configured row
      4. The same configuration for a different role is accepted
      5. A PATCH that would introduce the conflict is refused the same way
      6. A PATCH that claims no new slot is not refused — the rule reads the
         transition, not the end state
    """
    # ── Phase 1 ─────────────────────────────────────────────────────────
    first = create_fixed_key_provider(
        client,
        superuser_token_headers,
        name="Company OpenAI",
        auto_provision_roles=["agent-developer"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
    )

    # ── Phase 2 ─────────────────────────────────────────────────────────
    conflict = client.post(
        f"{PROVIDERS}/",
        headers=superuser_token_headers,
        json={
            "name": "Company Claude",
            "kind": "fixed_key",
            "type": "anthropic",
            "secret": FIXED_SECRET,
            "auto_provision_roles": ["agent-developer"],
            "set_user_sdk_defaults": True,
            "sdk_default_modes": ["building"],
        },
    )
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    # The field names are the contract: a dialog highlights the offending
    # role/mode cell and links to the other provider, and it can do neither
    # from a prose sentence.
    assert detail["code"] == "auto_provision_conflict"
    assert detail["conflicting_credential_id"] == first["id"]
    assert detail["conflicting_credential_name"] == "Company OpenAI"
    assert detail["role"] == "agent-developer"
    assert detail["mode"] == "building"
    assert detail["message"]

    # ── Phase 3 ─────────────────────────────────────────────────────────
    names = {row["name"] for row in client.get(
        f"{PROVIDERS}/", headers=superuser_token_headers
    ).json()}
    assert names == {"Company OpenAI"}

    # ── Phase 4 ─────────────────────────────────────────────────────────
    second = create_fixed_key_provider(
        client,
        superuser_token_headers,
        name="Company Claude",
        auto_provision_roles=["agent-user"],
        set_user_sdk_defaults=True,
        sdk_default_modes=["conversation", "building"],
    )

    # ── Phase 5 ─────────────────────────────────────────────────────────
    patched = client.patch(
        f"{PROVIDERS}/{second['id']}",
        headers=superuser_token_headers,
        json={"auto_provision_roles": ["agent-user", "agent-developer"]},
    )
    assert patched.status_code == 409, patched.text
    patched_detail = patched.json()["detail"]
    assert patched_detail["code"] == "auto_provision_conflict"
    assert patched_detail["conflicting_credential_id"] == first["id"]
    assert patched_detail["role"] == "agent-developer"
    # Refused before a single field was written.
    assert get_provider(client, superuser_token_headers, second["id"])[
        "auto_provision_roles"
    ] == ["agent-user"]

    # ── Phase 6 ─────────────────────────────────────────────────────────
    # The rule is scoped to slots the request *newly* claims. A provider sitting
    # in a slot-claiming configuration must stay editable: renaming it, or
    # re-sending its own configuration verbatim from a dialog opened earlier,
    # claims nothing new and is not re-validated against the stored state.
    renamed = client.patch(
        f"{PROVIDERS}/{second['id']}",
        headers=superuser_token_headers,
        json={"name": "Company Claude (EU)"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Company Claude (EU)"
    assert renamed.json()["sdk_default_modes"] == ["conversation", "building"]

    restated = client.patch(
        f"{PROVIDERS}/{second['id']}",
        headers=superuser_token_headers,
        json={
            "auto_provision_roles": ["agent-user"],
            "set_user_sdk_defaults": True,
            "sdk_default_modes": ["conversation", "building"],
        },
    )
    assert restated.status_code == 200, restated.text


# ── The retired managed-credential fields ───────────────────────────────────


def test_a_misspelled_config_field_is_refused_not_dropped(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``extra="forbid"`` has to reach the nested object, not stop at the top.

    ``config`` is where ``project_id`` lives, and ``project_id`` is what selects
    the OpenAI project keys are minted into and whose spend limit bounds them.
    Dropped silently, the provider verifies as uncapped and refuses to mint, and
    nothing anywhere says a field was thrown away.

    ``organisation_id`` is the specific typo worth pinning rather than a
    made-up one: the field is spelled the American way and this codebase's own
    prose spells it the British way throughout ("provider organisation",
    "organisation administration secret"), so it is the mistake a reader of the
    docs makes.

    Asserted on both verbs, because the refusal lives on a shape both share and
    a regression could plausibly reach only one of them.
    """
    refused = client.post(
        f"{PROVIDERS}/",
        headers=superuser_token_headers,
        json={
            "name": f"Typo {random_lower_string()[:8]}",
            "kind": "minted",
            "type": "openai",
            "secret": ADMIN_SECRET,
            "config": {"project_id": "proj_test", "organisation_id": "org_1"},
        },
    )
    assert refused.status_code == 422, refused.text
    assert any(
        "organisation_id" in str(error.get("loc", ""))
        for error in refused.json()["detail"]
    ), refused.text

    existing = create_minted_provider(client, superuser_token_headers)
    patched = client.patch(
        f"{PROVIDERS}/{existing['id']}",
        headers=superuser_token_headers,
        json={"config": {"projekt_id": "proj_typo"}},
    )
    assert patched.status_code == 422, patched.text

    # The control: the correct spelling is still accepted and still stored, so
    # this is a refusal of the typo rather than of the field.
    ok = client.patch(
        f"{PROVIDERS}/{existing['id']}",
        headers=superuser_token_headers,
        json={"config": {"project_id": "proj_moved", "organization_id": "org_1"}},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["config"]["project_id"] == "proj_moved"
    assert ok.json()["config"]["organization_id"] == "org_1"


def test_a_stray_member_key_on_a_provider_create_is_refused_not_ignored(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A provider has one secret field, and a second one must not be swallowed.

    ``api_key`` is the spelling every other credential surface in this codebase
    uses, so it is the obvious thing to reach for when connecting a provider —
    and for a ``minted`` provider it is precisely the wrong field, because the
    one secret there *is* the administration key and a member key has no slot at
    all. Ignored, the request answers 200 and the admin believes they pasted
    something that was never stored. ``AIProviderCreate`` forbids unknown keys,
    so it is a 422 naming the field.

    The control matters as much as the refusal: the identical body without the
    stray key still creates the provider. Otherwise this passes for a body that
    was malformed for some entirely different reason.
    """
    body = {
        "name": f"Stray key {random_lower_string()[:8]}",
        "kind": "minted",
        "type": "openai",
        "secret": ADMIN_SECRET,
        "config": {"project_id": "proj_test"},
    }

    refused = client.post(
        f"{PROVIDERS}/",
        headers=superuser_token_headers,
        json={**body, "api_key": "sk-a-member-key"},
    )
    assert refused.status_code == 422, refused.text
    assert any(
        "api_key" in str(error.get("loc", ""))
        for error in refused.json()["detail"]
    ), refused.text

    accepted = client.post(
        f"{PROVIDERS}/", headers=superuser_token_headers, json=body
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["has_secret"] is True


def test_the_retired_managed_credential_fields_are_refused_not_ignored(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Removing a field from a Pydantic model does not refuse it — it ignores it.

    ``auto_provision_roles``, ``provisioning_mode`` and
    ``provider_admin_credential_id`` moved off the managed credential when the
    provider became the record that owns them. A client still sending one would
    otherwise get a 200 and nothing would happen at the next signup: the
    "saved successfully, changed nothing" failure the provider/credential split
    exists to delete, arriving through the door the split just closed.
    ``extra="forbid"`` is what makes it a 422 naming the field instead.

    The ``[]`` case on PATCH is the documented gap this closes: an empty
    ``auto_provision_roles`` on a provider-owned record used to be accepted,
    write nothing anywhere, and leave an administrator believing they had
    stopped that provider auto-provisioning.

    Referenced by name from ``ManagedAICredentialCreate``'s docstring.
    """
    provider = create_fixed_key_provider(client, superuser_token_headers)

    def _create(**extra) -> object:
        return client.post(
            f"{MANAGED}/",
            headers=superuser_token_headers,
            json={
                "name": f"Manual {random_lower_string()[:8]}",
                "type": "anthropic",
                "api_key": "sk-ant-manual-not-real",
                "target_user_ids": [],
                **extra,
            },
        )

    refused = {
        "auto_provision_roles-nonempty": (
            _create(auto_provision_roles=["admin"]),
            "auto_provision_roles",
        ),
        "auto_provision_roles-empty": (
            _create(auto_provision_roles=[]),
            "auto_provision_roles",
        ),
        "provisioning_mode": (
            _create(provisioning_mode="minted"),
            "provisioning_mode",
        ),
        "provider_admin_credential_id": (
            _create(provider_admin_credential_id=provider["id"]),
            "provider_admin_credential_id",
        ),
    }
    for label, (response, field) in refused.items():
        assert response.status_code == 422, (label, response.text)
        locations = {
            tuple(str(part) for part in error["loc"])
            for error in response.json()["detail"]
        }
        assert any(field in loc for loc in locations), (label, locations)

    # ── The control: the same request without the retired field lands ───
    accepted = _create()
    assert accepted.status_code == 200, accepted.text
    manual_id = accepted.json()["record"]["id"]

    # ── PATCH: the ``[]`` gap, on the record it was specific to ─────────
    for target, label in (
        (provider["owned_credential_id"], "provider-owned"),
        (manual_id, "manual"),
    ):
        response = client.patch(
            f"{MANAGED}/{target}",
            headers=superuser_token_headers,
            json={"auto_provision_roles": []},
        )
        assert response.status_code == 422, (label, response.text)
        locations = {
            tuple(str(part) for part in error["loc"])
            for error in response.json()["detail"]
        }
        assert any("auto_provision_roles" in loc for loc in locations), (
            label,
            locations,
        )

    # And a PATCH that says nothing retired still works, so the 422s above are
    # about the field rather than about the route.
    renamed = client.patch(
        f"{MANAGED}/{manual_id}",
        headers=superuser_token_headers,
        json={"name": "Renamed manual"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["record"]["name"] == "Renamed manual"


def test_a_provider_owned_credential_says_so_and_a_manual_one_does_not(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """``is_provider_owned`` is the **answer**, not ``provider_id !== null``.

    The two agree today; only one of them keeps agreeing the day the rule
    changes, and it is the browser that would keep answering the old way.
    ``provider_name`` is there so the fleet table can render a Source column
    without a second request per row.
    """
    provider = create_fixed_key_provider(
        client, superuser_token_headers, name="Company Claude"
    )

    owned = client.get(
        f"{MANAGED}/{provider['owned_credential_id']}",
        headers=superuser_token_headers,
    )
    assert owned.status_code == 200, owned.text
    assert owned.json()["is_provider_owned"] is True
    assert owned.json()["provider_id"] == provider["id"]
    assert owned.json()["provider_name"] == "Company Claude"

    created = client.post(
        f"{MANAGED}/",
        headers=superuser_token_headers,
        json={
            "name": "Manual record",
            "type": "anthropic",
            "api_key": "sk-ant-manual-not-real",
            "target_user_ids": [],
        },
    )
    assert created.status_code == 200, created.text
    manual = client.get(
        f"{MANAGED}/{created.json()['record']['id']}",
        headers=superuser_token_headers,
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["is_provider_owned"] is False
    assert manual.json()["provider_id"] is None
    assert manual.json()["provider_name"] is None

    # Both are listed by the fleet table, and each carries its own answer.
    listing = {
        row["id"]: row
        for row in client.get(f"{MANAGED}/", headers=superuser_token_headers).json()
    }
    assert listing[provider["owned_credential_id"]]["is_provider_owned"] is True
    assert listing[created.json()["record"]["id"]]["is_provider_owned"] is False
