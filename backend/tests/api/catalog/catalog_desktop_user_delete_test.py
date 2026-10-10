"""Deleting a person who created desktop agents.

``agent_bundle.publisher_user_id`` is RESTRICT, so both deletion routes first
detach the person from their desktop agents. The agents stay in the catalog
without a publisher; their grants for that person go with the user; a
superuser still manages them (P2), and other grant holders keep their access.

Scenarios:
  1. ``DELETE /users/me`` by the creator of a desktop agent.
  2. ``DELETE /users/{id}`` by a superuser.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.catalog import (
    create_desktop_agent,
    get_item,
    list_items,
    make_person,
    patch_item,
    user_grant,
)

API = settings.API_V1_STR


def _check_orphaned(client: TestClient, superuser_headers: dict, item: dict, holder) -> None:
    got = get_item(client, superuser_headers, item["uuid"])
    assert got["publisher"] is None
    assert got["maintainers"] == []  # the creator's maintain row went with them
    # Superusers manage it (P2); a remaining holder still sees it.
    assert patch_item(client, superuser_headers, item["uuid"], display_name="Kept")[
        "display_name"
    ] == "Kept"
    assert item["uuid"] in {
        i["uuid"] for i in list_items(client, holder.headers, runs_on="desktop")
    }
    assert get_item(client, holder.headers, item["uuid"])["access"] == "edit"


def test_self_delete_detaches_desktop_agents(
    client: TestClient, superuser_token_headers
) -> None:
    """
    1. A person creates a desktop agent and shares it (edit) with a colleague
    2. They delete their own account -> 200, not a server error
    3. The agent stays, ownerless, still reachable by the colleague
    """
    creator = make_person(client, superuser_token_headers)
    colleague = make_person(client, superuser_token_headers)
    item = create_desktop_agent(
        client, creator.headers, grants=[user_grant(colleague.id, "edit")]
    )

    r = client.delete(f"{API}/users/me", headers=creator.headers)
    assert r.status_code == 200, r.text

    _check_orphaned(client, superuser_token_headers, item, colleague)


def test_superuser_delete_detaches_desktop_agents(
    client: TestClient, superuser_token_headers
) -> None:
    """
    1. A person creates two desktop agents (one shared with a colleague)
    2. A superuser deletes the person -> 200
    3. Both agents remain; the shared one keeps the colleague's access
    """
    creator = make_person(client, superuser_token_headers)
    colleague = make_person(client, superuser_token_headers)
    shared = create_desktop_agent(
        client, creator.headers, visibility="users",
        grants=[user_grant(colleague.id, "edit")],
    )
    private = create_desktop_agent(client, creator.headers)

    r = client.delete(f"{API}/users/{creator.id}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text

    _check_orphaned(client, superuser_token_headers, shared, colleague)
    assert get_item(client, superuser_token_headers, private["uuid"])["publisher"] is None
