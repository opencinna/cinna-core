"""Catalog responses label credential slots without leaking template values.

Bug 7 of the catalog-unification follow-up: ``GET /api/v1/catalog/items``
returned each item's raw ``required_credential_specs``, including a template
credential's ``template_data`` (the publisher's shipped field values), to
anyone with ``use``. Catalog responses now carry only what labels a slot and
who fills it: ``name``, ``type``, ``description``, ``provided_by``,
``publisher_credential_id``, ``service_uri`` (plus the skill producer fields).
``template_data``, ``template_private_fields`` and the consent flags
``allow_sharing`` / ``allow_template_sharing`` stay on the revision.

Scenario: a publisher links a template-sharing Odoo credential and a plain
API-token credential, publishes, and makes the bundle public. The publish
response (the publisher's own revision) still carries ``template_data``; a
consumer with ``use`` reads the same specs through every catalog surface and
sees only the label keys:
  - ``GET /catalog/items`` (unified listing)
  - ``GET /catalog/items/{uuid}`` (unified detail)
  - ``GET /catalog/`` and ``GET /catalog/{bundle_id}`` (cloud-bundle catalog)

Further scenarios:
  2. A skill package item (``GET /catalog/items`` + detail): label keys only.
  3. A template credential whose ``template_private_fields`` lists
     ``service_uri``: catalog responses omit ``service_uri`` for that slot
     entirely, while a template slot with a public ``service_uri`` keeps it
     (bundle and skill items).
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    create_bundle_credential,
    link_bundle_credential_to_agent,
    make_bundle_public,
    publish_bundle_revision,
)
from tests.utils.catalog import get_item, list_items
from tests.utils.credential import (
    create_random_credential,
    link_credential_to_agent,
    update_credential,
)
from tests.utils.skill_catalog import (
    make_agent_with_env,
    publish_skill,
    write_skill_with_credentials,
)

API = settings.API_V1_STR

HIDDEN_KEYS = {
    "template_data",
    "template_private_fields",
    "allow_sharing",
    "allow_template_sharing",
}
LABEL_KEYS = {
    "name",
    "type",
    "description",
    "provided_by",
    "publisher_credential_id",
    "service_uri",
    "producer_agent_id",
    "producer_agent_name",
}


def _create_template_odoo_credential(
    client: TestClient, headers: dict[str, str], name: str
) -> dict:
    r = client.post(
        f"{API}/credentials/",
        headers=headers,
        json={
            "name": name,
            "type": "odoo",
            "allow_sharing": False,
            "allow_template_sharing": True,
            "template_private_fields": ["login", "api_token"],
            "credential_data": {
                "url": "https://erp.example.com",
                "database_name": "shipped_db",
                "login": "publisher_login",
                "api_token": "publisher_token",
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _assert_label_only(specs: list[dict], *, where: str) -> dict[str, dict]:
    """Every spec carries only label keys; returns them keyed by name."""
    assert specs, f"{where}: expected credential specs"
    for spec in specs:
        leaked = HIDDEN_KEYS & set(spec)
        assert not leaked, f"{where}: spec {spec.get('name')!r} leaks {leaked}"
        assert set(spec) <= LABEL_KEYS, f"{where}: unexpected keys {set(spec) - LABEL_KEYS}"
        assert {"name", "type", "provided_by"} <= set(spec), f"{where}: {spec}"
        # Nothing of the publisher's credential values, under any key.
        assert "shipped_db" not in str(spec) and "publisher_token" not in str(spec)
    return {s["name"]: s for s in specs}


def test_catalog_responses_project_credential_specs_to_label_keys(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Publisher links a template Odoo credential and an API-token credential,
       publishes (the revision keeps template_data), makes the bundle public.
    2. A consumer reads the unified listing and detail: label keys only.
    3. The cloud-bundle catalog list and entry: label keys only.
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    consumer = make_person(client, su, developer=False)
    suffix = uuid.uuid4().hex[:6]
    odoo_name = f"odoo-{suffix}"
    token_name = f"token-{suffix}"

    # ── Phase 1: publish with a template credential ───────────────────────
    odoo_id = _create_template_odoo_credential(client, publisher.headers, odoo_name)["id"]
    token_cred = create_bundle_credential(
        client, publisher.headers, name=token_name, allow_sharing=True
    )
    agent = create_agent_via_api(client, publisher.headers, name=f"specs-{suffix}")
    drain_tasks()
    link_bundle_credential_to_agent(client, publisher.headers, agent["id"], odoo_id)
    link_bundle_credential_to_agent(client, publisher.headers, agent["id"], token_cred["id"])

    revision = publish_bundle_revision(client, publisher.headers, agent["id"])
    raw = {s["name"]: s for s in revision["required_credential_specs"]}
    assert raw[odoo_name]["provided_by"] == "template"
    # The revision itself still has what the install flow needs.
    assert raw[odoo_name]["template_data"] == {
        "url": "https://erp.example.com",
        "database_name": "shipped_db",
    }
    assert sorted(raw[odoo_name]["template_private_fields"]) == ["api_token", "login"]
    assert "allow_sharing" in raw[token_name]

    fresh = client.get(f"{API}/agents/{agent['id']}", headers=publisher.headers).json()
    bundle_uuid, bundle_id = fresh["bundle_uuid"], fresh["bundle_id"]
    make_bundle_public(client, publisher.headers, bundle_uuid)

    def _check(specs: list[dict], where: str) -> None:
        by_name = _assert_label_only(specs, where=where)
        assert set(by_name) == {odoo_name, token_name}, where
        assert by_name[odoo_name]["type"] == "odoo"
        assert by_name[odoo_name]["provided_by"] == "template"
        assert by_name[odoo_name]["publisher_credential_id"] is None
        assert by_name[token_name]["type"] == "api_token"
        assert by_name[token_name]["provided_by"] == raw[token_name]["provided_by"]
        assert (
            by_name[token_name]["publisher_credential_id"]
            == raw[token_name]["publisher_credential_id"]
        )
        for name in (odoo_name, token_name):
            assert by_name[name]["service_uri"] == raw[name]["service_uri"], where
            assert by_name[name]["description"] == raw[name]["description"], where

    # ── Phase 2: unified listing + detail ─────────────────────────────────
    listed = next(
        i for i in list_items(client, consumer.headers) if i["uuid"] == bundle_uuid
    )
    _check(listed["required_credential_specs"], "GET /catalog/items")
    detail = get_item(client, consumer.headers, bundle_uuid)
    _check(detail["required_credential_specs"], "GET /catalog/items/{uuid}")

    # The publisher's own view of the catalog is projected the same way.
    own = get_item(client, publisher.headers, bundle_uuid)
    _assert_label_only(own["required_credential_specs"], where="publisher detail")

    # ── Phase 3: cloud-bundle catalog ─────────────────────────────────────
    r = client.get(f"{API}/catalog/", headers=consumer.headers)
    assert r.status_code == 200, r.text
    entry = next(e for e in r.json()["data"] if e["bundle_uuid"] == bundle_uuid)
    _check(entry["required_credential_specs"], "GET /catalog/")

    r = client.get(f"{API}/catalog/{bundle_id}", headers=consumer.headers)
    assert r.status_code == 200, r.text
    _check(r.json()["required_credential_specs"], "GET /catalog/{bundle_id}")


# ── Scenario 2/3: skill packages, and a private service_uri ───────────────


def _template_api_token_credential(
    client: TestClient,
    headers: dict[str, str],
    service_uri: str,
    *,
    service_uri_private: bool,
) -> dict:
    cred = create_random_credential(client, headers, credential_type="api_token")
    private = ["api_token", "http_header_value"]
    if service_uri_private:
        private.append("service_uri")
    update_credential(
        client,
        headers,
        cred["id"],
        service_uri=service_uri,
        allow_template_sharing=True,
        template_private_fields=private,
    )
    return cred


def test_skill_item_specs_are_label_only_and_a_private_service_uri_is_omitted(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A publisher's skill declares two api_token slots, each filled by a
       template credential: one with a public service_uri, one that marks
       service_uri private. Published public.
    2. A consumer reads the skill item on GET /catalog/items and its detail:
       label keys only; the public slot keeps service_uri, the private one
       carries no service_uri key at all.
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    consumer = make_person(client, su, developer=False)
    suffix = uuid.uuid4().hex[:6]
    public_slot, private_slot = f"pub-{suffix}", f"priv-{suffix}"

    agent_id, env_id = make_agent_with_env(client, publisher.headers, f"skill-src-{suffix}")
    for slot, is_private in ((public_slot, False), (private_slot, True)):
        cred = _template_api_token_credential(
            client, publisher.headers, slot, service_uri_private=is_private
        )
        link_credential_to_agent(client, publisher.headers, agent_id, cred["id"])
    skill_name = f"specs-skill-{suffix}"
    write_skill_with_credentials(
        env_id,
        skill_name,
        [
            {"slot": public_slot, "type": "api_token"},
            {"slot": private_slot, "type": "api_token"},
        ],
    )
    revision = publish_skill(
        client, publisher.headers, agent_id, skill_name, visibility="public"
    )
    package_uuid = revision["package_id"]
    assert {c["provided_by"] for c in revision["required_credentials"]} == {"template"}

    listed = next(
        i for i in list_items(client, consumer.headers) if i["uuid"] == package_uuid
    )
    detail = get_item(client, consumer.headers, package_uuid)
    for where, specs in (
        ("GET /catalog/items (skill)", listed["required_credential_specs"]),
        ("GET /catalog/items/{uuid} (skill)", detail["required_credential_specs"]),
    ):
        by_name = _assert_label_only(specs, where=where)
        assert set(by_name) == {public_slot, private_slot}, where
        for spec in by_name.values():
            assert spec["type"] == "api_token", where
            assert spec["provided_by"] == "template", where
        assert by_name[public_slot]["service_uri"] == public_slot, where
        assert "service_uri" not in by_name[private_slot], (
            f"{where}: a private service_uri must be omitted: {by_name[private_slot]}"
        )
        assert private_slot not in str(
            {k: v for k, v in by_name[private_slot].items() if k != "name"}
        ), where


def test_bundle_template_spec_with_private_service_uri_omits_it_in_the_catalog(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A publisher links two template Odoo credentials, one marking
       service_uri private, and publishes a public bundle; the revision itself
       still records both service_uris.
    2. On every catalog read the private one has no service_uri key; the
       public one keeps its value.
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    consumer = make_person(client, su, developer=False)
    suffix = uuid.uuid4().hex[:6]
    public_name, private_name = f"odoo-pub-{suffix}", f"odoo-priv-{suffix}"
    public_uri, private_uri = f"erp-public-{suffix}", f"erp-private-{suffix}"

    agent = create_agent_via_api(client, publisher.headers, name=f"uri-{suffix}")
    drain_tasks()
    for name, uri, is_private in (
        (public_name, public_uri, False),
        (private_name, private_uri, True),
    ):
        cred_id = _create_template_odoo_credential(client, publisher.headers, name)["id"]
        private = ["login", "api_token"] + (["service_uri"] if is_private else [])
        update_credential(
            client,
            publisher.headers,
            cred_id,
            service_uri=uri,
            template_private_fields=private,
        )
        link_bundle_credential_to_agent(client, publisher.headers, agent["id"], cred_id)

    revision = publish_bundle_revision(client, publisher.headers, agent["id"])
    raw = {s["name"]: s for s in revision["required_credential_specs"]}
    assert raw[public_name]["provided_by"] == raw[private_name]["provided_by"] == "template"
    assert raw[public_name]["service_uri"] == public_uri
    assert "service_uri" in raw[private_name]["template_private_fields"]

    fresh = client.get(f"{API}/agents/{agent['id']}", headers=publisher.headers).json()
    bundle_uuid, bundle_id = fresh["bundle_uuid"], fresh["bundle_id"]
    make_bundle_public(client, publisher.headers, bundle_uuid)

    reads = {
        "GET /catalog/items": next(
            i for i in list_items(client, consumer.headers) if i["uuid"] == bundle_uuid
        )["required_credential_specs"],
        "GET /catalog/items/{uuid}": get_item(
            client, consumer.headers, bundle_uuid
        )["required_credential_specs"],
        "GET /catalog/": next(
            e
            for e in client.get(f"{API}/catalog/", headers=consumer.headers).json()["data"]
            if e["bundle_uuid"] == bundle_uuid
        )["required_credential_specs"],
        "GET /catalog/{bundle_id}": client.get(
            f"{API}/catalog/{bundle_id}", headers=consumer.headers
        ).json()["required_credential_specs"],
    }
    for where, specs in reads.items():
        by_name = _assert_label_only(specs, where=where)
        assert by_name[public_name]["service_uri"] == public_uri, where
        assert "service_uri" not in by_name[private_name], (
            f"{where}: a private service_uri must be omitted: {by_name[private_name]}"
        )
        assert private_uri not in str(by_name[private_name]), where
