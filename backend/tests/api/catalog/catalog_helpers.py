"""HTTP helpers for the catalog domain tests (``tests/api/catalog/``).

Kept beside the tests while the domain is being built (C1–C5); move to
``tests/utils/catalog.py`` once another domain needs them.
"""
from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import publish_bundle
from tests.utils.skill_catalog import (
    make_agent_with_env,
    make_developer,
    publish_skill,
    write_skill,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


@dataclass
class Person:
    id: str
    email: str
    headers: dict[str, str]


def make_person(
    client: TestClient, superuser_headers: dict[str, str], *, developer: bool = True
) -> Person:
    """A fresh user: an ``agent-developer`` with a default AI credential, or a
    plain ``agent-user`` (``developer=False``)."""
    if developer:
        user, headers = make_developer(client, superuser_headers)
    else:
        user, headers = create_random_user_with_headers(client)
    return Person(id=user["id"], email=user["email"], headers=headers)


def make_non_admin_superuser(
    client: TestClient, superuser_headers: dict[str, str]
) -> Person:
    """A superuser whose ``role`` is ``agent-developer`` — so a match on the
    ``admin`` role grant can only come from ``is_superuser``."""
    person = make_person(client, superuser_headers)
    r = client.patch(
        f"{API}/users/{person.id}",
        headers=superuser_headers,
        json={"is_superuser": True, "role": "agent-developer"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_superuser"] is True
    assert r.json()["role"] == "agent-developer"
    return person


# ── Grants body builders ──────────────────────────────────────────────────


def user_grant(user_id: str, access: str) -> dict:
    return {"principal": {"user_id": user_id}, "access": access}


def role_grant(role: str, access: str) -> dict:
    return {"principal": {"role": role}, "access": access}


# ── /catalog/items/{uuid}/grants ──────────────────────────────────────────


def get_item_grants(
    client: TestClient,
    headers: dict[str, str],
    item_uuid: str,
    *,
    expected_status: int = 200,
) -> list[dict] | dict:
    r = client.get(f"{API}/catalog/items/{item_uuid}/grants", headers=headers)
    assert r.status_code == expected_status, (
        f"get grants: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def put_item_grants(
    client: TestClient,
    headers: dict[str, str],
    item_uuid: str,
    grants: list[dict],
    *,
    expected_status: int = 200,
) -> list[dict] | dict:
    r = client.put(
        f"{API}/catalog/items/{item_uuid}/grants", headers=headers, json=grants
    )
    assert r.status_code == expected_status, (
        f"put grants: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def grant_levels(grants: list[dict]) -> dict[str, str]:
    """``{"user:<id>" | "role:<role>": access}`` of a grants listing."""
    out: dict[str, str] = {}
    for g in grants:
        p = g["principal"]
        key = f"user:{p['user_id']}" if "user_id" in p else f"role:{p['role']}"
        out[key] = g["access"]
    return out


# ── Bundles ───────────────────────────────────────────────────────────────


def publish_agent_bundle(
    client: TestClient, headers: dict[str, str], name: str
) -> dict:
    """Create an agent, publish it; return ``{agent_id, bundle_uuid, bundle_id}``."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    fresh = publish_bundle(client, headers, agent["id"])
    assert fresh["bundle_uuid"], fresh
    bundle = client.get(f"{API}/bundles/{fresh['bundle_uuid']}", headers=headers)
    assert bundle.status_code == 200, bundle.text
    return {
        "agent_id": agent["id"],
        "bundle_uuid": fresh["bundle_uuid"],
        "bundle_id": bundle.json()["bundle_id"],
    }


def set_bundle_visibility(
    client: TestClient,
    headers: dict[str, str],
    bundle_uuid: str,
    *,
    visibility: str,
    is_listed: bool = True,
) -> None:
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"visibility": visibility, "is_listed": is_listed},
    )
    assert r.status_code == 200, r.text


def bundle_catalog_uuids(client: TestClient, headers: dict[str, str]) -> set[str]:
    r = client.get(f"{API}/catalog/", headers=headers)
    assert r.status_code == 200, r.text
    return {e["bundle_uuid"] for e in r.json()["data"]}


# ── Skills ────────────────────────────────────────────────────────────────


def publish_skill_package(
    client: TestClient,
    headers: dict[str, str],
    skill_name: str,
    *,
    visibility: str | None = None,
) -> dict:
    """Seed ``skills/<name>`` in a new agent and publish it; return
    ``{agent_id, env_id, package_uuid, package_id}``."""
    agent_id, env_id = make_agent_with_env(client, headers, f"src-{skill_name}")
    write_skill(env_id, skill_name)
    revision = publish_skill(client, headers, agent_id, skill_name, visibility=visibility)
    package = client.get(
        f"{API}/skills/packages/{revision['package_id']}", headers=headers
    )
    assert package.status_code == 200, package.text
    return {
        "agent_id": agent_id,
        "env_id": env_id,
        "package_uuid": revision["package_id"],
        "package_id": package.json()["package_id"],
    }
