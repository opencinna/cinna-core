"""Publishing a bundle that links a credential shared WITH the publisher (P0, plan §1).

Bug: ``resolve_provided_by`` used to infer ``publisher`` for any shareable
credential, but only the owner may provide it at install. Every install then
silently degraded to ``publisher_unavailable``. Now the inference goes through
``publisher_may_provide`` and an explicit ``publisher`` override on a
credential the publisher may not provide is rejected at publish.

Scenarios:
  A. Shared agent_api connection, re-use off: drift ``live`` says ``user`` with
     ``blocked_reason="reuse_off"``; an explicit ``publisher`` override fails
     publish (400, names the owner); clearing it publishes ``user``.
  B. The owner's own shareable credential still resolves ``publisher``
     (regression) and ``live`` reports it as providable.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import link_bundle_credential_to_agent
from tests.utils.credential import share_credential_via_api, update_credential

API = settings.API_V1_STR


def _shared_connection(client, owner_headers, recipient_email) -> dict:
    producer = setup_producer(client, owner_headers, name=f"SharedPub-{uuid.uuid4().hex[:4]}")
    r = client.post(
        f"{API}/agents/{producer['id']}/agent-api/connect", headers=owner_headers, json={}
    )
    assert r.status_code == 200, r.text
    cid = r.json()["credential_id"]
    update_credential(client, owner_headers, cid, allow_sharing=True)
    if recipient_email:
        share_credential_via_api(client, owner_headers, cid, recipient_email)
    return client.get(f"{API}/credentials/{cid}", headers=owner_headers).json()


def _drift(client, headers, agent_id) -> dict:
    r = client.get(f"{API}/agents/{agent_id}/bundle-credential-drift", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _publish_raw(client, headers, agent_id):
    return client.post(f"{API}/agents/{agent_id}/publish", headers=headers, json={})


def _set_overrides(client, headers, agent_id, overrides) -> None:
    r = client.patch(
        f"{API}/agents/{agent_id}/publish-settings",
        headers=headers,
        json={"credential_overrides": overrides},
    )
    assert r.status_code == 200, r.text


def _specs(client, headers, agent_id) -> list[dict]:
    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    ctx = client.get(f"{API}/catalog/{fresh['bundle_id']}/install-context", headers=headers)
    assert ctx.status_code == 200, ctx.text
    return ctx.json()["service_specs"]


def test_publishing_a_credential_shared_with_you_does_not_embed_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. S shares an agent_api connection with T; T links it to a new agent
    2. Drift ``live``: resolved ``user``, blocked ``reuse_off``, cannot be provided
    3. T sets override ``publisher`` -> live reports it as-is, publish is 400
       and names S
    4. T clears the override -> publish succeeds with ``provided_by == "user"``
    """
    su = superuser_token_headers
    s, sh = make_developer(client, su)
    t, th = make_developer(client, su)
    cred = _shared_connection(client, sh, t["email"])
    cid, cname = cred["id"], cred["name"]

    agent = create_agent_via_api(client, th, name="SharedPub-T-Agent")
    drain_tasks()
    # publish-settings is publisher-install only: the first publish makes it one.
    assert _publish_raw(client, th, agent["id"]).status_code == 200
    drain_tasks()
    link_bundle_credential_to_agent(client, th, agent["id"], cid)

    # ── Phase 2: live provisioning value ─────────────────────────────
    drift = _drift(client, th, agent["id"])
    live = {o["credential_id"]: o for o in drift["live"]}
    assert live[cid]["resolved_provided_by"] == "user"
    assert live[cid]["blocked_reason"] == "reuse_off"
    assert "publisher" not in live[cid]["can_provide_as"]

    # ── Phase 3: explicit publisher override is rejected at publish ──
    _set_overrides(client, th, agent["id"], {cname: {"provided_by": "publisher"}})
    live = {o["credential_id"]: o for o in _drift(client, th, agent["id"])["live"]}
    assert live[cid]["resolved_provided_by"] == "publisher"
    assert live[cid]["blocked_reason"] == "reuse_off"
    r = _publish_raw(client, th, agent["id"])
    assert r.status_code == 400, r.text
    assert (s["full_name"] or s["email"]) in r.json()["detail"]
    assert "re-use" in r.json()["detail"]

    # ── Phase 4: clear the override -> publishes as user ─────────────
    _set_overrides(client, th, agent["id"], {})
    r = _publish_raw(client, th, agent["id"])
    assert r.status_code == 200, r.text
    drain_tasks()
    specs = _specs(client, th, agent["id"])
    assert [sp["provided_by"] for sp in specs] == ["user"], specs


def test_owned_shareable_credential_still_resolves_publisher_with_live_option(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The owner publishing their own shareable connection is unchanged."""
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    cred = _shared_connection(client, oh, None)
    agent = create_agent_via_api(client, oh, name="SharedPub-Owner-Agent")
    drain_tasks()
    link_bundle_credential_to_agent(client, oh, agent["id"], cred["id"])

    live = {o["credential_id"]: o for o in _drift(client, oh, agent["id"])["live"]}
    assert live[cred["id"]]["resolved_provided_by"] == "publisher"
    assert live[cred["id"]]["blocked_reason"] is None
    assert "publisher" in live[cred["id"]]["can_provide_as"]

    r = _publish_raw(client, oh, agent["id"])
    assert r.status_code == 200, r.text
    drain_tasks()
    assert [sp["provided_by"] for sp in _specs(client, oh, agent["id"])] == ["publisher"]


def test_foreign_template_credential_is_not_offered_and_publish_as_template_is_rejected(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A credential shared with T is never T's to ship as a template.

    1. S shares an odoo credential (template-shareable) with T; T links it
    2. Drift ``live`` does not offer ``template``; an unrelated stranger gets 404
    3. T sets a ``template`` override: publish is 400 and names S
    """
    su = superuser_token_headers
    s, sh = make_developer(client, su)
    t, th = make_developer(client, su)
    _, stranger_h = make_developer(client, su)
    r = client.post(
        f"{API}/credentials/",
        headers=sh,
        json={
            "name": f"foreign-tpl-{uuid.uuid4().hex[:6]}",
            "type": "odoo",
            "allow_sharing": True,
            "allow_template_sharing": True,
            "template_private_fields": ["api_token"],
            "credential_data": {
                "url": "https://erp.example.com",
                "database_name": "db",
                "login": "admin",
                "api_token": "secret",
            },
        },
    )
    assert r.status_code == 200, r.text
    cred = r.json()
    share_credential_via_api(client, sh, cred["id"], t["email"])
    agent = create_agent_via_api(client, th, name="ForeignTpl-T-Agent")
    drain_tasks()
    assert _publish_raw(client, th, agent["id"]).status_code == 200
    drain_tasks()
    link_bundle_credential_to_agent(client, th, agent["id"], cred["id"])

    drift = _drift(client, th, agent["id"])
    live = {o["credential_id"]: o for o in drift["live"]}
    assert "template" not in live[cred["id"]]["can_provide_as"]
    r = client.get(f"{API}/agents/{agent['id']}/bundle-credential-drift", headers=stranger_h)
    assert r.status_code == 404

    _set_overrides(client, th, agent["id"], {cred["name"]: {"provided_by": "template"}})
    r = _publish_raw(client, th, agent["id"])
    assert r.status_code == 400, r.text
    assert (s["full_name"] or s["email"]) in r.json()["detail"]


def test_publisher_override_saved_before_first_publish_is_honoured_for_reusable_shared_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A never-published agent can hold a ``publisher`` override.

    1. S shares a connection with T (re-use on); T links it to a new agent
    2. T saves a ``publisher`` override before any publish: 200
    3. First publish succeeds and the spec is ``publisher``
    """
    su = superuser_token_headers
    _, sh = make_developer(client, su)
    t, th = make_developer(client, su)
    cred = _shared_connection(client, sh, t["email"])
    update_credential(client, sh, cred["id"], allow_bundle_reuse=True)

    agent = create_agent_via_api(client, th, name="SharedPub-T-NeverPublished")
    drain_tasks()
    link_bundle_credential_to_agent(client, th, agent["id"], cred["id"])

    _set_overrides(client, th, agent["id"], {cred["name"]: {"provided_by": "publisher"}})

    r = _publish_raw(client, th, agent["id"])
    assert r.status_code == 200, r.text
    drain_tasks()
    specs = _specs(client, th, agent["id"])
    assert [sp["provided_by"] for sp in specs] == ["publisher"], specs
