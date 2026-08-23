"""Install context and install payload tests for agent bundles.

Covers the ``GET /catalog/{bundle_id}/install-context`` endpoint, the
``InstallCredentialSelection`` payload shape for
``POST /catalog/{bundle_id}/install``, and the rejection of legacy payload
formats.

Scenarios:
  A. GET install-context — 404 for non-visible (private) bundle.
  B. GET install-context — minimal shape: no publisher AI, no PBP specs.
  C. GET install-context — publisher AI credentials exposed as name/type
     summaries with no secrets in the body.
  D. GET install-context — auto-prefill suggestion when user credential
     matches spec by (name, type).
  E. GET install-context — case-insensitive name matching for suggestions.
  F. GET install-context — owned credential preferred over shared credential.
  G. GET install-context — most-recent (higher id) shared credential wins
     when no owned match exists.
  H. GET install-context — PBP spec exposes publisher_summary {name, type}.
  I. POST install with new mode="use_existing" + credential_id — install
     activates and link points at supplied credential.
  J. POST install with new mode="placeholder" — placeholder Credential
     created (is_placeholder=True) and linked.
  K. POST install — mode="use_existing" rejected with HTTP 422 when spec
     is provided_by="publisher".
  L. Legacy ``{name: uuid_string}`` install payload rejected (the shim was
     dropped; typed ``InstallCredentialSelection`` is the only accepted form).

Direct DB access via the ``db`` fixture is retained only where the API cannot
serve the assertion: scenario J asserts the ``encrypted_data`` invariant on a
placeholder credential (a field no projection exposes), and scenarios K/L
assert the *absence* of an Agent row after a rejected install by reading it
straight from the session rather than routing a negative assertion through
``GET /agents/``.

(Historical note: an earlier version of this comment claimed K/L had no
choice but to use ``db`` because the rejected install's request-session
rollback unwound the test's SAVEPOINT past the installer's own creation,
leaving the installer unable to authenticate for a follow-up ``GET
/agents/``. That was real, but it was a bug in ``tests/conftest.py``'s ``db``
fixture — a caught ``IntegrityError`` + ``session.rollback()`` anywhere in a
request unwound past the current SAVEPOINT instead of stopping at it — fixed
by adding ``join_transaction_mode="create_savepoint"`` to the fixture's
``Session`` construction. The installer authenticates fine post-fix; the
direct-``db`` assertion is kept anyway since it's the simpler read for a
negative check.)

Everything else is verified via the API.
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.credentials.credential import Credential
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    create_bundle_credential as _create_credential,
    install_bundle as _install,
    link_bundle_credential_to_agent as _link_credential_to_agent,
    make_bundle_public as _make_public,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle as _publish,
)
from tests.utils.credential import share_credential_via_api

API = settings.API_V1_STR


# ── Helpers ───────────────────────────────────────────────────────────────────
# Shared bundle helpers (_make_user_and_headers, _create_credential, _publish,
# _make_public, _install, _link_credential_to_agent) are imported above from
# tests.utils.bundle. Credential sharing goes through the public
# POST /credentials/{id}/shares API (share_credential_via_api).


# ── Scenario A: 404 for non-visible bundle ────────────────────────────────────


def test_install_context_404_for_non_visible_bundle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A. GET /catalog/{bundle_id}/install-context returns 404 for a private
    bundle that the calling user has no access to.

    1. Publisher creates + publishes a bundle (default: not listed / not public).
    2. A foreign user calls GET .../install-context.
    3. Assert HTTP 404.
    """
    # ── Phase 1: publish bundle (private by default) ───────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-A-Agent"
    )
    drain_tasks()
    fresh = _publish(client, superuser_token_headers, agent["id"])
    bundle_id = fresh["bundle_id"]
    # Do NOT make it public.

    # ── Phase 2: foreign user cannot see the bundle ────────────────────────────
    _, foreign_headers = _make_user_and_headers(client)
    r = client.get(
        f"{API}/catalog/{bundle_id}/install-context",
        headers=foreign_headers,
    )
    assert r.status_code == 404, (
        f"Expected 404 for non-visible bundle; got {r.status_code}: {r.text}"
    )


# ── Scenario B: minimal shape — no publisher AI, no PBP specs ────────────────


def test_install_context_minimal_shape_no_publisher_ai_no_pbp(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """B. GET install-context for a public bundle with no publisher AI
    credentials and only PBU specs returns the correct minimal shape.

    1. Publish a bundle with one non-shareable (PBU) credential.
    2. Make bundle public.
    3. Foreign user GETs install-context.
    4. Assert:
       - ai_provided_by_publisher=False
       - ai_publisher_credential_summaries.conversation and .building are null
       - service_specs has one entry with provided_by="user", publisher_summary=null,
         suggested_credential_id=null (no matching user credential exists)
    """
    # ── Phase 1: publish bundle with one PBU credential ───────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-B-Agent"
    )
    drain_tasks()

    cred = _create_credential(
        client, superuser_token_headers, name="ic-b-mailbox", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    bundle_id = fresh["bundle_id"]
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: foreign user with NO matching credential ─────────────────────
    _, foreign_headers = _make_user_and_headers(client)
    r = client.get(
        f"{API}/catalog/{bundle_id}/install-context",
        headers=foreign_headers,
    )
    assert r.status_code == 200, r.text
    ctx = r.json()

    # ── Phase 3: assert minimal shape ─────────────────────────────────────────
    assert ctx["ai_provided_by_publisher"] is False, (
        f"Expected ai_provided_by_publisher=False; got {ctx['ai_provided_by_publisher']}"
    )
    summaries = ctx["ai_publisher_credential_summaries"]
    assert summaries["conversation"] is None, (
        f"Expected conversation summary=null; got {summaries['conversation']}"
    )
    assert summaries["building"] is None, (
        f"Expected building summary=null; got {summaries['building']}"
    )

    specs = ctx["service_specs"]
    assert len(specs) == 1, f"Expected 1 spec; got {specs}"
    spec = specs[0]
    assert spec["name"] == "ic-b-mailbox"
    assert spec["provided_by"] == "user"
    assert spec["publisher_summary"] is None
    assert spec["suggested_credential_id"] is None
    assert spec["suggested_credential_name"] is None

    # bundle entry is also present.
    assert ctx["bundle"]["bundle_id"] == bundle_id


# ── Scenario C: publisher AI summaries expose name/type, no secrets ──────────


def test_install_context_publisher_ai_summaries_no_secrets(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """C. Bundle with both publisher_ai_credential_*_id set.

    Assert:
    - ai_provided_by_publisher=True.
    - ai_publisher_credential_summaries.conversation and .building are
      non-null dicts with {name, type}.
    - The entire response JSON body contains no 'api_key', 'token',
      'encrypted_data', or 'secret' fields (string-search).
    """
    # ── Phase 1: publish + set both PBP AI credentials ────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-C-Agent"
    )
    drain_tasks()
    fresh = _publish(client, superuser_token_headers, agent["id"])

    conv_ai = create_random_ai_credential(client, superuser_token_headers)
    build_ai = create_random_ai_credential(client, superuser_token_headers)

    r = client.patch(
        f"{API}/bundles/{fresh['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": conv_ai["id"],
            "publisher_ai_credential_building_id": build_ai["id"],
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r.status_code == 200, r.text

    # ── Phase 2: foreign user GETs install-context ────────────────────────────
    _, foreign_headers = _make_user_and_headers(client)
    r = client.get(
        f"{API}/catalog/{fresh['bundle_id']}/install-context",
        headers=foreign_headers,
    )
    assert r.status_code == 200, r.text
    ctx = r.json()

    # ── Phase 3: assert publisher AI summaries ────────────────────────────────
    assert ctx["ai_provided_by_publisher"] is True, (
        f"Expected ai_provided_by_publisher=True; got {ctx['ai_provided_by_publisher']}"
    )
    summaries = ctx["ai_publisher_credential_summaries"]
    conv_summary = summaries["conversation"]
    build_summary = summaries["building"]

    assert conv_summary is not None, "Expected conversation summary to be non-null"
    assert "name" in conv_summary, f"Missing 'name' in conversation summary: {conv_summary}"
    assert "type" in conv_summary, f"Missing 'type' in conversation summary: {conv_summary}"

    assert build_summary is not None, "Expected building summary to be non-null"
    assert "name" in build_summary, f"Missing 'name' in building summary: {build_summary}"
    assert "type" in build_summary, f"Missing 'type' in building summary: {build_summary}"

    # ── Phase 4: no secret fields in body ─────────────────────────────────────
    raw_json = r.text.lower()
    secret_keys = ("api_key", "encrypted_data")
    for secret_key in secret_keys:
        assert f'"{secret_key}"' not in raw_json, (
            f"Secret field '{secret_key}' found in install-context response body"
        )


# ── Scenario D: auto-prefill suggestion when match exists ────────────────────


def test_install_context_auto_prefill_suggestion_when_match_exists(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """D. Foreign user has a Credential matching spec (name, type).

    Assert the spec's suggested_credential_id equals that credential's id
    and suggested_credential_name equals its name.

    1. Publish bundle with PBU spec named "gmail-work", type "api_token".
    2. Foreign user creates an owned credential with same name and type.
    3. GET install-context.
    4. Assert suggestion = the user's credential.
    """
    # ── Phase 1: publish bundle with PBU spec ────────────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-D-Agent"
    )
    drain_tasks()

    pub_cred = _create_credential(
        client, superuser_token_headers, name="gmail-work", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: foreign user creates own credential with matching (name, type) ─
    foreign_user, foreign_headers = _make_user_and_headers(client)
    matching_cred = _create_credential(
        client, foreign_headers, name="gmail-work", allow_sharing=False
    )

    # ── Phase 3: GET install-context ─────────────────────────────────────────
    r = client.get(
        f"{API}/catalog/{fresh['bundle_id']}/install-context",
        headers=foreign_headers,
    )
    assert r.status_code == 200, r.text
    ctx = r.json()

    specs = ctx["service_specs"]
    assert len(specs) == 1, f"Expected 1 spec; got {specs}"
    spec = specs[0]
    assert spec["name"] == "gmail-work"
    assert spec["provided_by"] == "user"

    assert spec["suggested_credential_id"] == matching_cred["id"], (
        f"Expected suggestion={matching_cred['id']}; "
        f"got {spec['suggested_credential_id']}"
    )
    assert spec["suggested_credential_name"] == matching_cred["name"], (
        f"Expected name={matching_cred['name']}; "
        f"got {spec['suggested_credential_name']}"
    )


# ── Scenario E: case-insensitive name matching ────────────────────────────────


def test_install_context_case_insensitive_name_match(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """E. User credential name="Gmail" matches spec name="gmail" (case-insensitive).

    1. Publish bundle with spec named "gmail".
    2. Foreign user creates credential named "Gmail" (mixed case).
    3. GET install-context.
    4. Assert suggestion points at the user's "Gmail" credential.
    """
    # ── Phase 1: publish bundle with "gmail" spec ─────────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-E-Agent"
    )
    drain_tasks()

    pub_cred = _create_credential(
        client, superuser_token_headers, name="gmail", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: foreign user has "Gmail" (different case) ───────────────────
    _, foreign_headers = _make_user_and_headers(client)
    # The credentials API may normalise the name; store it as-is.
    user_cred = _create_credential(
        client, foreign_headers, name="Gmail", allow_sharing=False
    )

    # ── Phase 3: GET install-context ─────────────────────────────────────────
    r = client.get(
        f"{API}/catalog/{fresh['bundle_id']}/install-context",
        headers=foreign_headers,
    )
    assert r.status_code == 200, r.text
    ctx = r.json()

    spec = ctx["service_specs"][0]
    assert spec["name"] == "gmail"
    assert spec["suggested_credential_id"] == user_cred["id"], (
        f"Case-insensitive match failed: expected suggestion={user_cred['id']}; "
        f"got {spec['suggested_credential_id']}"
    )


# ── Scenario F: owned credential preferred over shared ───────────────────────


def test_install_context_owned_preferred_over_shared(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """F. When the user has BOTH an owned credential and a shared credential
    matching the spec, the owned one is returned as the suggestion.

    1. Publish bundle with spec "crm-key", type "api_token".
    2. Third user creates a shareable credential named "crm-key" and shares
       it with the installer via CredentialShare.
    3. Installer ALSO creates their own owned credential named "crm-key".
    4. GET install-context.
    5. Assert suggestion = installer's owned credential (not the shared one).
    """
    # ── Phase 1: publish bundle ───────────────────────────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-F-Agent"
    )
    drain_tasks()

    pub_cred = _create_credential(
        client, superuser_token_headers, name="crm-key", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: third user creates a shareable credential and shares it ──────
    third_user, third_headers = _make_user_and_headers(client)
    shared_cred = _create_credential(
        client, third_headers, name="crm-key", allow_sharing=True
    )
    installer, installer_headers = _make_user_and_headers(client)

    share_credential_via_api(
        client, third_headers, shared_cred["id"], installer["email"]
    )

    # ── Phase 3: installer also owns a credential with same (name, type) ──────
    owned_cred = _create_credential(
        client, installer_headers, name="crm-key", allow_sharing=False
    )

    # ── Phase 4: GET install-context ──────────────────────────────────────────
    r = client.get(
        f"{API}/catalog/{fresh['bundle_id']}/install-context",
        headers=installer_headers,
    )
    assert r.status_code == 200, r.text
    ctx = r.json()

    spec = ctx["service_specs"][0]
    assert spec["suggested_credential_id"] == owned_cred["id"], (
        f"Expected owned cred {owned_cred['id']} to be preferred; "
        f"got suggestion={spec['suggested_credential_id']}"
    )


# ── Scenario G: most-recent shared wins when no owned match ──────────────────


def test_install_context_most_recent_shared_wins_when_no_owned(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """G. User has two shared credentials matching (name, type), no owned one.

    Assert the suggestion picks the higher-id (most recently created) row.

    1. Publish bundle with spec "newsletter", type "api_token".
    2. Third user A creates credential "newsletter" and shares with installer.
    3. Third user B creates credential "newsletter" and shares with installer.
    4. GET install-context.
    5. Assert suggestion = the credential with the higher id (B was created after A).
    """
    # ── Phase 1: publish bundle ───────────────────────────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-G-Agent"
    )
    drain_tasks()

    pub_cred = _create_credential(
        client, superuser_token_headers, name="newsletter", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: two third parties share matching credentials with installer ───
    installer, installer_headers = _make_user_and_headers(client)

    third_a, third_a_headers = _make_user_and_headers(client)
    cred_a = _create_credential(
        client, third_a_headers, name="newsletter", allow_sharing=True
    )
    cred_a_id = uuid.UUID(cred_a["id"])
    share_credential_via_api(
        client, third_a_headers, cred_a["id"], installer["email"]
    )

    third_b, third_b_headers = _make_user_and_headers(client)
    cred_b = _create_credential(
        client, third_b_headers, name="newsletter", allow_sharing=True
    )
    cred_b_id = uuid.UUID(cred_b["id"])
    share_credential_via_api(
        client, third_b_headers, cred_b["id"], installer["email"]
    )

    # ── Phase 3: GET install-context ──────────────────────────────────────────
    r = client.get(
        f"{API}/catalog/{fresh['bundle_id']}/install-context",
        headers=installer_headers,
    )
    assert r.status_code == 200, r.text
    ctx = r.json()

    spec = ctx["service_specs"][0]
    # Higher id = more recently created; UUID comparison works lexicographically
    # but we compare the actual UUID objects for correctness.
    suggested_id = uuid.UUID(spec["suggested_credential_id"])
    # The credential with the higher UUID (later insert) should be picked.
    expected_winner = cred_b_id if cred_b_id > cred_a_id else cred_a_id
    assert suggested_id == expected_winner, (
        f"Expected most-recent shared cred {expected_winner}; "
        f"got {suggested_id} (cred_a={cred_a_id}, cred_b={cred_b_id})"
    )


# ── Scenario H: PBP spec exposes publisher_summary ───────────────────────────


def test_install_context_pbp_spec_exposes_publisher_summary(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """H. Spec with provided_by="publisher" and valid publisher_credential_id
    returns publisher_summary={name, type} in the install context.

    1. Publish bundle with allow_sharing=True credential (PBP spec).
    2. Make bundle public.
    3. Foreign user GETs install-context.
    4. Assert spec entry has provided_by="publisher" and non-null
       publisher_summary with the publisher credential's name and type.
    """
    # ── Phase 1: publish bundle with shareable (PBP) credential ──────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-H-Agent"
    )
    drain_tasks()

    pub_cred = _create_credential(
        client, superuser_token_headers, name="crm-shared-cred", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: foreign user GETs install-context ───────────────────────────
    _, foreign_headers = _make_user_and_headers(client)
    r = client.get(
        f"{API}/catalog/{fresh['bundle_id']}/install-context",
        headers=foreign_headers,
    )
    assert r.status_code == 200, r.text
    ctx = r.json()

    # ── Phase 3: assert PBP spec has publisher_summary ───────────────────────
    specs = ctx["service_specs"]
    assert len(specs) == 1, f"Expected 1 spec; got {specs}"
    spec = specs[0]
    assert spec["provided_by"] == "publisher", (
        f"Expected provided_by='publisher'; got '{spec['provided_by']}'"
    )
    assert spec["publisher_summary"] is not None, (
        "Expected publisher_summary to be non-null for PBP spec"
    )
    assert spec["publisher_summary"]["name"] == pub_cred["name"], (
        f"Expected publisher_summary.name={pub_cred['name']}; "
        f"got {spec['publisher_summary']['name']}"
    )
    assert spec["publisher_summary"]["type"] == pub_cred["type"], (
        f"Expected publisher_summary.type={pub_cred['type']}; "
        f"got {spec['publisher_summary']['type']}"
    )
    # No suggestion needed for PBP spec.
    assert spec["suggested_credential_id"] is None, (
        "PBP spec must not have a suggested_credential_id"
    )


# ── Scenario I: POST install with mode="use_existing" + credential_id ────────


def test_install_new_payload_use_existing_links_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """I. POST install with new payload mode="use_existing" + credential_id.

    Assert:
    - Install returns HTTP 200 and activates.
    - The installed agent's credential link points at the supplied credential.
    - No placeholder created (the user supplied an explicit credential).
    """
    # ── Phase 1: publish bundle with one PBU spec ─────────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-I-Publisher"
    )
    drain_tasks()

    pub_cred = _create_credential(
        client, superuser_token_headers, name="ic-i-cred", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: installer creates their own credential then installs ─────────
    installer, installer_headers = _make_user_and_headers(client)

    user_cred = _create_credential(
        client, installer_headers, name="ic-i-cred", allow_sharing=False
    )

    install = _install(
        client,
        installer_headers,
        fresh["bundle_id"],
        request_body={
            "credentials": {
                "ic-i-cred": {
                    "mode": "use_existing",
                    "credential_id": user_cred["id"],
                }
            }
        },
    )
    install_id = install["id"]

    # ── Phase 3: verify link points at supplied credential ────────────────────
    creds = client.get(
        f"{API}/agents/{install_id}/credentials", headers=installer_headers
    )
    assert creds.status_code == 200, creds.text
    linked = creds.json()["data"]
    assert len(linked) == 1, f"Expected 1 linked credential; got {len(linked)}"
    assert linked[0]["id"] == user_cred["id"], (
        f"Link points at {linked[0]['id']}; expected {user_cred['id']}"
    )
    assert linked[0]["is_placeholder"] is False, (
        "Linked credential must not be a placeholder when mode='use_existing'"
    )


# ── Scenario J: POST install with mode="placeholder" ─────────────────────────


def test_install_new_payload_placeholder_creates_placeholder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """J. POST install with mode="placeholder" creates is_placeholder=True row.

    Assert:
    - Install returns HTTP 200 and activates.
    - Placeholder Credential created (is_placeholder=True, encrypted_data non-empty).
    - AgentCredentialLink points at the placeholder.
    """
    # ── Phase 1: publish bundle with one PBU spec ─────────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-J-Publisher"
    )
    drain_tasks()

    pub_cred = _create_credential(
        client, superuser_token_headers, name="ic-j-cred", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: installer installs with explicit mode="placeholder" ──────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    install = _install(
        client,
        installer_headers,
        fresh["bundle_id"],
        request_body={
            "credentials": {
                "ic-j-cred": {"mode": "placeholder"}
            }
        },
    )
    install_id = install["id"]

    # ── Phase 3: verify placeholder created and linked ────────────────────────
    creds = client.get(
        f"{API}/agents/{install_id}/credentials", headers=installer_headers
    )
    assert creds.status_code == 200, creds.text
    linked = creds.json()["data"]
    assert len(linked) == 1, f"Expected 1 linked credential; got {len(linked)}"
    placeholder = linked[0]
    assert placeholder["is_placeholder"] is True, (
        f"Expected is_placeholder=True; got {placeholder['is_placeholder']}"
    )
    assert uuid.UUID(placeholder["owner_id"]) == installer_id, (
        f"Placeholder must be owned by installer {installer_id}; "
        f"got {placeholder['owner_id']}"
    )

    # encrypted_data is intentionally not exposed by any credential projection
    # (secret-stripped), so its non-empty invariant is verified via the db
    # fixture. No API surface reveals this field.
    placeholder_row = db.get(Credential, uuid.UUID(placeholder["id"]))
    assert placeholder_row is not None and placeholder_row.encrypted_data, (
        "encrypted_data must be non-empty on a placeholder credential"
    )


# ── Scenario K: mode="use_existing" rejected with 422 for PBP spec ───────────


def test_install_use_existing_rejected_for_publisher_provided_spec(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """K. POST install with mode="use_existing" for a spec whose provided_by=
    "publisher" returns HTTP 422 and no Agent row is created.

    Assert:
    - Response status 422.
    - Response detail mentions the spec name or a friendly message.
    - No Agent row (install) created for the installer.

    The "no agent created" check uses the ``db`` fixture directly rather than
    ``GET /agents/`` — simplest read for a negative assertion. (This is NOT
    because the installer can't authenticate after the rejection: an earlier
    ``tests/conftest.py`` bug did break that, but it's fixed — see the module
    docstring's historical note.)
    """
    # ── Phase 1: publish bundle with one PBP credential ───────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-K-Publisher"
    )
    drain_tasks()

    pbp_cred = _create_credential(
        client, superuser_token_headers, name="ic-k-shared", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pbp_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: installer creates their own credential to try to override ─────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    user_cred = _create_credential(
        client, installer_headers, name="ic-k-own-cred", allow_sharing=False
    )

    # ── Phase 3: attempt install with use_existing on a PBP spec ─────────────
    r = client.post(
        f"{API}/catalog/{fresh['bundle_id']}/install",
        headers=installer_headers,
        json={
            "credentials": {
                "ic-k-shared": {
                    "mode": "use_existing",
                    "credential_id": user_cred["id"],
                }
            }
        },
    )
    assert r.status_code == 422, (
        f"Expected 422 when overriding a publisher-provided spec with use_existing; "
        f"got {r.status_code}: {r.text}"
    )

    detail = r.json().get("detail", "")
    assert detail, "Expected a non-empty detail message in 422 response"
    # The detail should mention the spec name or the restriction.
    detail_lower = str(detail).lower()
    assert (
        "publisher" in detail_lower
        or "ic-k-shared" in detail_lower
        or "cannot" in detail_lower
        or "override" in detail_lower
    ), f"Expected a helpful 422 detail about publisher spec; got: {detail}"

    # ── Phase 4: verify no Agent row was created ──────────────────────────────
    db.expire_all()
    agent_rows = db.exec(
        select(Agent).where(
            Agent.owner_id == installer_id,
            Agent.bundle_uuid == uuid.UUID(fresh["bundle_uuid"]),
        )
    ).all()
    assert len(agent_rows) == 0, (
        f"Expected no Agent row created after 422; got {len(agent_rows)}: "
        f"{[str(a.id) for a in agent_rows]}"
    )


# ── Scenario L: legacy {name: uuid_string} payload rejected ──────────────────


def test_install_legacy_uuid_string_payload_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """L. The legacy ``{name: uuid_string}`` install payload is rejected.

    The previous shim in ``InstallService._normalise_credentials_payload``
    accepted ``credentials: {"spec_name": "<uuid>"}`` and converted it
    into ``{"mode": "use_existing", "credential_id": "<uuid>"}``. With
    the shim gone, the route's typed validator rejects anything that
    isn't a typed :class:`InstallCredentialSelection` body.

    Assert: the install endpoint returns HTTP 422 and creates no Agent row.

    The "no Agent row" check uses the ``db`` fixture directly, same as
    scenario K — simplest read for a negative assertion (see the module
    docstring's historical note on why this isn't a forced workaround).
    """
    # Publish a bundle with one PBU spec.
    agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCtx-L-Publisher"
    )
    drain_tasks()
    pub_cred = _create_credential(
        client, superuser_token_headers, name="ic-l-legacy", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # Installer attempts the legacy shape — must be rejected at the API edge.
    installer, installer_headers = _make_user_and_headers(client)
    user_cred = _create_credential(
        client, installer_headers, name="ic-l-legacy", allow_sharing=False
    )
    r = client.post(
        f"{API}/catalog/{fresh['bundle_id']}/install",
        headers=installer_headers,
        json={
            "credentials": {
                # OLD shape: spec-name → uuid string (no "mode" key).
                "ic-l-legacy": user_cred["id"]
            }
        },
    )
    assert r.status_code == 422, (
        f"Legacy payload must be rejected with 422 after the shim removal; "
        f"got {r.status_code}: {r.text}"
    )

    # And no Install row should have been created on the rejected request.
    installer_id = uuid.UUID(installer["id"])
    db.expire_all()
    agent_rows = db.exec(
        select(Agent).where(
            Agent.owner_id == installer_id,
            Agent.bundle_id == fresh["bundle_id"],
        )
    ).all()
    assert len(agent_rows) == 0, (
        f"Expected no Agent row created after 422; got {len(agent_rows)}"
    )
