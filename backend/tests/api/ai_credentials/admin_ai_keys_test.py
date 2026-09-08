"""The keys list and its per-key verbs — ``/admin/ai-credentials/keys``.

WHAT THIS SURFACE IS FOR, AND THEREFORE WHAT THESE TESTS PROTECT
----------------------------------------------------------------
**One row = one real API key.** A ``minted`` record contributes one row per
member, because each of them has a key of their own at the vendor; a shared
record contributes exactly one, because its N child credentials all carry the
same secret. The row count follows the number of secrets that exist at the
provider, and the first two scenarios below are that arithmetic asserted rather
than described.

Everything else here is a per-key verb. Until they existed, acting on one
person's key meant PATCHing the record with the whole desired member set — so
the tests that matter most are the ones asserting that a verb aimed at one key
leaves every other key alone.

Provider I/O is replaced through the **registry override**, never by patching a
module attribute, so the real provisioner runs with only its HTTP swapped. See
``tests/stubs/key_provisioner_stub.py``.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.ai_provider import stub_minting_providers
from tests.utils.background_tasks import drain_tasks
from tests.utils.fixtures import (
    BACKGROUND_TASK_TARGETS_FULL,
    patched_background_tasks,
    patched_create_sessions,
)
from tests.utils.key_provisioning import (
    converge_keys,
    mark_membership_minting,
    membership_id_for,
)
from tests.utils.query_counter import count_queries
from tests.utils.managed_ai_credential import (
    configure_minted_record,
    connect_minted_provider,
    create_managed_credential,
)
from tests.utils.user import create_random_user_with_headers

# No agents or environments here. The revoke path schedules a provider call in
# the background, which is the one thing off the request path this file needs.
NEEDS_AGENT_STUBS = False
NEEDS_DEFAULT_CREDENTIALS = False

API = settings.API_V1_STR
KEYS_BASE = f"{API}/admin/ai-credentials/keys"
MANAGED_BASE = f"{API}/admin/llm-providers"


@pytest.fixture(autouse=True)
def background_and_sessions(db: Session):
    """Collect background tasks and pin the services that open their own session.

    The same pair ``minted_ai_credentials_test`` declares, and for the same
    reason: a revocation is handed to the background loop with a session of its
    own, which under the test transaction would see none of this test's data.
    """
    with patched_create_sessions(db), patched_background_tasks(
        BACKGROUND_TASK_TARGETS_FULL
    ):
        yield


# ── Helpers ─────────────────────────────────────────────────────────────────


def _new_user(client: TestClient) -> dict:
    user, headers = create_random_user_with_headers(client)
    user["headers"] = headers
    return user


def _keys(
    client: TestClient, headers: dict[str, str], **params: object
) -> dict:
    response = client.get(f"{KEYS_BASE}/", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _row_for(page: dict, email: str) -> dict | None:
    for row in page["data"]:
        if row.get("holder_email") == email:
            return row
    return None


def _minted_record_with(
    client: TestClient, headers: dict[str, str], db: Session, *users: dict
) -> str:
    """A minted provider whose members all hold a key. Returns the record id."""
    provider_id = connect_minted_provider(client, headers)
    result = configure_minted_record(
        client,
        headers,
        provider_id=provider_id,
        target_user_ids=[str(user["id"]) for user in users],
    )
    with stub_minting_providers():
        converge_keys(db)
    return result["record"]["id"]


# ── One row = one real key ──────────────────────────────────────────────────


def test_a_minted_record_contributes_one_row_per_member(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Three members of one minted record are three keys, not one record.

    This is the whole reason the surface exists: the record list answers "what
    records exist" and shows one row here, which said nothing about the three
    secrets that were created at the vendor.
    """
    users = [_new_user(client) for _ in range(3)]
    _minted_record_with(client, superuser_token_headers, db, *users)

    page = _keys(client, superuser_token_headers)
    assert page["count"] == 3, page
    assert len(page["data"]) == 3
    assert {row["holder_email"] for row in page["data"]} == {
        user["email"] for user in users
    }
    for row in page["data"]:
        assert row["kind"] == "per_user"
        assert row["provisioning_status"] == "provisioned"
        assert row["child_credential_id"] is not None
        # The handles, so the row can be matched against the vendor's console —
        # and never the secret.
        assert row["key_reference"], row
        assert "sk-" not in row["key_reference"]
        assert row["member_count"] is None


def test_a_shared_record_is_one_row_however_many_hold_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Its N children are copies of one secret, so it is one key.

    A row per holder here would be the same mistake in the other direction: it
    would report five keys where the vendor issued one, and an admin revoking
    "one of them" would be destroying everybody's.
    """
    users = [_new_user(client) for _ in range(3)]
    create_managed_credential(
        client,
        superuser_token_headers,
        name="Shared Anthropic",
        target_user_ids=[str(user["id"]) for user in users],
    )

    page = _keys(client, superuser_token_headers)
    assert page["count"] == 1, page
    row = page["data"][0]
    assert row["kind"] == "shared"
    assert row["credential_name"] == "Shared Anthropic"
    assert row["member_count"] == 3
    # No holder and no provisioning lifecycle: this key was pasted, not minted.
    assert row["holder_email"] is None
    assert row["provisioning_status"] is None
    assert row["membership_id"] is None


def test_a_member_with_no_key_yet_is_still_a_row(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The list is keys *and* keys on the way.

    Hiding a ``pending`` member until their key materialises would make the
    surface silent at exactly the moment somebody has to look at it.
    """
    user = _new_user(client)
    provider_id = connect_minted_provider(client, superuser_token_headers)
    configure_minted_record(
        client,
        superuser_token_headers,
        provider_id=provider_id,
        target_user_ids=[str(user["id"])],
    )

    row = _row_for(_keys(client, superuser_token_headers), user["email"])
    assert row is not None
    assert row["provisioning_status"] == "pending"
    assert row["child_credential_id"] is None
    assert row["key_reference"] is None


# ── Paging, ordering, filters ───────────────────────────────────────────────


def test_shared_rows_come_first_and_paging_is_exact_across_the_boundary(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Every row appears exactly once across the pages that cover the list.

    The two row kinds come from two tables, so a page is a slice of their
    union. The bug this guards is the one every merge has: a row that shows up
    on two pages, or on none, as the offset crosses from the shared block into
    the per-user one.
    """
    users = [_new_user(client) for _ in range(4)]
    create_managed_credential(
        client, superuser_token_headers, name="Shared One",
        target_user_ids=[str(users[0]["id"])],
    )
    create_managed_credential(
        client, superuser_token_headers, name="Shared Two",
        target_user_ids=[str(users[0]["id"])],
    )
    _minted_record_with(client, superuser_token_headers, db, *users)

    total = _keys(client, superuser_token_headers)["count"]
    assert total == 6, "two shared records plus four minted keys"

    seen: list[str] = []
    for skip in range(0, total, 2):
        page = _keys(client, superuser_token_headers, skip=skip, limit=2)
        assert page["count"] == total
        seen.extend(
            row["membership_id"] or row["managed_credential_id"]
            for row in page["data"]
        )
    assert len(seen) == total, seen
    assert len(set(seen)) == total, "a row was served on two pages"

    first_page = _keys(client, superuser_token_headers, limit=2)
    assert [row["kind"] for row in first_page["data"]] == ["shared", "shared"]
    assert [row["credential_name"] for row in first_page["data"]] == [
        "Shared One",
        "Shared Two",
    ]


def test_search_matches_the_holder_or_the_credential(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """One box, both questions — "whose key is this" and "keys of what"."""
    users = [_new_user(client) for _ in range(2)]
    connect_minted_provider(client, superuser_token_headers)
    provider_id = connect_minted_provider(client, superuser_token_headers)
    configure_minted_record(
        client,
        superuser_token_headers,
        provider_id=provider_id,
        target_user_ids=[str(user["id"]) for user in users],
    )
    create_managed_credential(
        client, superuser_token_headers, name="Finance Anthropic",
        target_user_ids=[str(users[0]["id"])],
    )

    by_person = _keys(client, superuser_token_headers, q=users[0]["email"])
    assert by_person["count"] == 1, by_person
    assert by_person["data"][0]["holder_email"] == users[0]["email"]

    by_credential = _keys(client, superuser_token_headers, q="finance")
    assert by_credential["count"] == 1, by_credential
    assert by_credential["data"][0]["credential_name"] == "Finance Anthropic"

    assert _keys(client, superuser_token_headers, q="nothing-matches")["count"] == 0


def test_a_status_filter_excludes_every_shared_row(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A shared key has no provisioning lifecycle, so it matches no status.

    The alternative — inventing ``not_applicable`` for it so it could pass —
    would put a status on the wire that no membership row ever held.
    """
    user = _new_user(client)
    create_managed_credential(
        client, superuser_token_headers, target_user_ids=[str(user["id"])]
    )
    other = _new_user(client)
    _minted_record_with(client, superuser_token_headers, db, other)

    provisioned = _keys(
        client, superuser_token_headers, status="provisioned"
    )
    assert provisioned["count"] == 1, provisioned
    assert provisioned["data"][0]["holder_email"] == other["email"]

    shared_only = _keys(client, superuser_token_headers, kind="shared")
    assert shared_only["count"] == 1
    assert shared_only["data"][0]["kind"] == "shared"


def test_the_page_costs_the_same_however_many_keys_there_are(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The query count must not follow headcount.

    The list this replaced embedded every member of every record in one
    response, so its cost grew with the size of the company on every load. A
    per-row lookup here would reintroduce that in the one surface whose whole
    purpose is to work at that size.
    """
    two = [_new_user(client) for _ in range(2)]
    _minted_record_with(client, superuser_token_headers, db, *two)
    with count_queries(db, matching="managed_ai_credential_membership") as few:
        assert _keys(client, superuser_token_headers)["count"] == 2
    # A nonzero anchor: ``len(many) == len(few)`` is satisfied by ``0 == 0`` the
    # day the needle drifts away from the SQL SQLAlchemy emits.
    assert few, "the needle matched no statements — this test counts nothing"

    more = [_new_user(client) for _ in range(4)]
    _minted_record_with(client, superuser_token_headers, db, *more)
    with count_queries(db, matching="managed_ai_credential_membership") as many:
        assert _keys(client, superuser_token_headers)["count"] == 6
    assert len(many) == len(few), (few, many)

    # The second table, which is where an N+1 would hide: the children the page
    # names are fetched in one lookup, not one per row.
    with count_queries(db, matching="FROM ai_credential") as children:
        _keys(client, superuser_token_headers)
    assert children, "no child lookup ran — the needle drifted"
    assert len(children) <= 3, children


# ── Per-key verbs ───────────────────────────────────────────────────────────


def test_revoking_one_key_leaves_every_other_key_alone(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The verb the surface exists for.

    Before this route the only way to remove one person was to PATCH the record
    with the whole desired member set, which is why the blast-radius gate
    answers with a *list*.
    """
    keeper, leaver = _new_user(client), _new_user(client)
    _minted_record_with(client, superuser_token_headers, db, keeper, leaver)
    membership_id = membership_id_for(db, leaver["id"])

    response = client.delete(
        f"{KEYS_BASE}/{membership_id}", headers=superuser_token_headers
    )
    assert response.status_code == 200, response.text

    page = _keys(client, superuser_token_headers)
    assert page["count"] == 1
    assert page["data"][0]["holder_email"] == keeper["email"]

    # The person's own credential is gone with it; the keeper's is untouched.
    assert client.get(
        f"{API}/ai-credentials/", headers=leaver["headers"]
    ).json()["data"] == []
    assert (
        len(client.get(f"{API}/ai-credentials/", headers=keeper["headers"]).json()["data"])
        == 1
    )


def test_revoking_a_key_destroys_it_at_the_provider(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """A removed key must not stay live at the vendor.

    Asserted at the provider rather than at the row: a row that has gone tells
    you nothing about whether the secret it named was destroyed, and that gap is
    the one this whole feature is built to refuse.
    """
    user = _new_user(client)
    _minted_record_with(client, superuser_token_headers, db, user)
    membership_id = membership_id_for(db, user["id"])

    with stub_minting_providers() as (_probes, provisioning):
        response = client.delete(
            f"{KEYS_BASE}/{membership_id}", headers=superuser_token_headers
        )
        assert response.status_code == 200, response.text
        drain_tasks()
    assert provisioning.revoke_count == 1, provisioning.calls


def test_a_key_cannot_be_revoked_while_its_mint_is_in_flight(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Deleting the row would strand the key the provider is creating right now.

    The converge pass is inside a provider call and is about to write the
    handles onto this row; without them nothing can ever name — or destroy —
    the service account it made.
    """
    user = _new_user(client)
    provider_id = connect_minted_provider(client, superuser_token_headers)
    configure_minted_record(
        client,
        superuser_token_headers,
        provider_id=provider_id,
        target_user_ids=[str(user["id"])],
    )
    mark_membership_minting(db, user["id"])
    membership_id = membership_id_for(db, user["id"])

    response = client.delete(
        f"{KEYS_BASE}/{membership_id}", headers=superuser_token_headers
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["blocked"][0]["reason"] == "mint_in_flight"
    # Still a member, still on the list: a refused removal changes nothing.
    assert _row_for(_keys(client, superuser_token_headers), user["email"])


def test_forcing_a_revoke_does_not_override_an_in_flight_mint(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """``force`` lifts the bundle gate; it cannot lift a mint in flight.

    The trap this guards is a route that answers on ``blocked and not force``:
    the block still comes back, the row is still intact, and the admin is told
    "Key revoked successfully" about a key that is live at the provider.
    """
    user = _new_user(client)
    provider_id = connect_minted_provider(client, superuser_token_headers)
    configure_minted_record(
        client,
        superuser_token_headers,
        provider_id=provider_id,
        target_user_ids=[str(user["id"])],
    )
    mark_membership_minting(db, user["id"])

    response = client.delete(
        f"{KEYS_BASE}/{membership_id_for(db, user['id'])}?force=true",
        headers=superuser_token_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["blocked"][0]["reason"] == "mint_in_flight"
    assert _row_for(_keys(client, superuser_token_headers), user["email"])


def test_rotating_a_key_replaces_it_with_a_different_one(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Rotate is destroy-then-mint, and the new key is a *new* key.

    Asserted at the provider, in **one** stub block: the recorder numbers the
    service accounts it hands out, so a second block would start again at the
    first name and a rotation that changed nothing would pass.

    A rotate that quietly kept the old secret would look identical on the row,
    which is why the assertion is on the handles and the child, not the status.
    """
    user = _new_user(client)
    provider_id = connect_minted_provider(client, superuser_token_headers)
    configure_minted_record(
        client,
        superuser_token_headers,
        provider_id=provider_id,
        target_user_ids=[str(user["id"])],
    )
    with stub_minting_providers() as (_probes, provisioning):
        converge_keys(db)
        before = _row_for(_keys(client, superuser_token_headers), user["email"])
        assert before["provisioning_status"] == "provisioned"

        response = client.post(
            f"{KEYS_BASE}/{membership_id_for(db, user['id'])}/rotate",
            headers=superuser_token_headers,
        )
        assert response.status_code == 200, response.text
        drain_tasks()
        assert provisioning.revoke_count == 1, provisioning.calls

        # Between the two halves the holder has no key — the window the copy
        # warns about, asserted rather than assumed.
        waiting = _row_for(_keys(client, superuser_token_headers), user["email"])
        assert waiting["provisioning_status"] == "pending"
        assert waiting["child_credential_id"] is None

        converge_keys(db)
        provisioning.assert_minted(2)

    after = _row_for(_keys(client, superuser_token_headers), user["email"])
    assert after["provisioning_status"] == "provisioned"
    assert after["key_reference"] != before["key_reference"]
    assert after["child_credential_id"] != before["child_credential_id"]


def test_a_shared_key_cannot_be_rotated_here(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Rotating one holder's copy of a shared key is not a thing to allow.

    It would either give that person a key nobody else has or destroy the key
    everyone else is using. Replacing a pasted key is the provider's job, and
    the refusal says so.
    """
    user = _new_user(client)
    create_managed_credential(
        client, superuser_token_headers, target_user_ids=[str(user["id"])]
    )
    membership_id = membership_id_for(db, user["id"])

    response = client.post(
        f"{KEYS_BASE}/{membership_id}/rotate", headers=superuser_token_headers
    )
    assert response.status_code == 400, response.text
    assert "shared" in response.json()["detail"].lower()


def test_making_one_key_its_holders_default_touches_nobody_else(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """The per-person counterpart of *Set default for all*.

    The record-level verb overwrites everybody's choice; this one fixes the
    single person whose grant declined an occupied slot under incumbent-wins.
    """
    one, two = _new_user(client), _new_user(client)
    _minted_record_with(client, superuser_token_headers, db, one, two)

    # Nothing is anybody's default yet: the record does not claim the slot.
    assert all(
        not row["is_default"] for row in _keys(client, superuser_token_headers)["data"]
    )

    response = client.post(
        f"{KEYS_BASE}/{membership_id_for(db, one['id'])}/default",
        headers=superuser_token_headers,
    )
    assert response.status_code == 200, response.text

    page = _keys(client, superuser_token_headers)
    assert _row_for(page, one["email"])["is_default"] is True
    assert _row_for(page, two["email"])["is_default"] is False


def test_a_key_that_does_not_exist_yet_cannot_be_made_a_default(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """There is nothing to point the default at, and inventing one is worse.

    A placeholder credential holding no key would break the invariant every
    consumer of the credential table relies on — that every row that exists is
    usable.
    """
    user = _new_user(client)
    provider_id = connect_minted_provider(client, superuser_token_headers)
    configure_minted_record(
        client,
        superuser_token_headers,
        provider_id=provider_id,
        target_user_ids=[str(user["id"])],
    )
    pending = client.post(
        f"{KEYS_BASE}/{membership_id_for(db, user['id'])}/default",
        headers=superuser_token_headers,
    )
    assert pending.status_code == 400, pending.text

    unknown = client.post(
        f"{KEYS_BASE}/{uuid.uuid4()}/default", headers=superuser_token_headers
    )
    assert unknown.status_code == 404, unknown.text


def test_every_verb_is_superuser_only(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A key list is a list of who holds what; it is not for its holders."""
    user = _new_user(client)
    _minted_record_with(client, superuser_token_headers, db, user)
    membership_id = membership_id_for(db, user["id"])

    assert client.get(f"{KEYS_BASE}/", headers=user["headers"]).status_code == 403
    assert (
        client.delete(
            f"{KEYS_BASE}/{membership_id}", headers=user["headers"]
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{KEYS_BASE}/{membership_id}/rotate", headers=user["headers"]
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{KEYS_BASE}/{membership_id}/default", headers=user["headers"]
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{KEYS_BASE}/{membership_id}/retry", headers=user["headers"]
        ).status_code
        == 403
    )
