"""C5 — skill credentials for local (Desktop) installs.

A desktop upload publish names, per credential slot, which of the publisher's
own credentials provides it (``meta.publisher_credentials``): a credential that
allows sharing freezes as ``provided_by='publisher'``, one that only allows
template sharing as ``'template'``; every unlisted slot is ``'user'``.
``POST /skills/packages/{uuid}/revisions/{n}/provision-local`` then shares each
``publisher`` slot with the caller (``CredentialShare.source =
'skill_local_install'``) so their Desktop can copy it through
``/external/credentials``. It never returns credential data.

Scenarios:
  1. Publish freezes ``publisher`` / ``template`` / ``user`` specs from the
     named credentials; an unlisted matching shareable credential stays
     ``user``; preview lists candidates; invalid choices are 422.
  2. The outcomes matrix for a ``use`` grantee (shared / not_allowed_locally
     / user_provides), the publisher (already_shared), idempotency, stranger
     and missing revision 404.
  3. The share reaches the recipient's Desktop (``/external/credentials``)
     and the web list as "automatic"; a stranger's Desktop sees nothing.
  4. Consent changes after publish: local use off → not_allowed_locally;
     sharing off, slot moved, credential deleted → not_shareable.
  5. U7: a caller with no grant on the skill, who can use a desktop agent
     pinning the revision, can provision; before the pin (or for another
     revision) it is 404.
  Edge cases (kept separate; the reviewer may change these rules):
  6. A pre-existing direct share → already_shared, source unchanged.
  7. A pre-existing ``skill_install`` share → already_shared and promoted to
     ``skill_local_install`` (now delivered locally).
  8. A pre-existing ``bundle_install`` share → not_allowed_locally, no id.
  9. An editor's republish naming a credential → 422; without one it works.
"""
import io
import json
import tarfile
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person as make_developer
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    create_bundle_credential,
    install_bundle,
    link_bundle_credential_to_agent,
    make_bundle_public,
    publish_bundle,
)
from tests.utils.catalog import (
    GitClient,
    create_desktop_agent,
    get_item,
    make_person,
    manifest_id_of,
    repo_id_of,
    server_contract_version,
    user_grant,
    write_agent_tree,
)
from tests.utils.credential import (
    link_credential_to_agent,
    set_credential_sharing,
    share_credential_via_api,
    update_credential,
)
from tests.utils.desktop_credentials import (
    create_credential_via_api,
    desktop_session,
    list_desktop_credentials,
    materialize_desktop_credentials,
)
from tests.utils.skill_catalog import (
    error_code,
    install_skill,
    make_agent_with_env,
    publish_skill,
    write_skill_with_credentials,
)

API = settings.API_V1_STR
PUBLISH = f"{API}/skills/packages/publish"
PREVIEW = f"{API}/skills/packages/publish-preview"
SLOT_KEYS = {"slot", "type", "provided_by", "outcome", "credential_id"}


# ── Helpers ────────────────────────────────────────────────────────────────


def _skill_tgz(name: str, slots: list[tuple[str, str]], body: str = "Step one.") -> bytes:
    """``<name>/SKILL.md`` declaring ``slots`` as ``(slot, type)`` pairs."""
    creds = "".join(f"  - slot: {slot}\n    type: {kind}\n" for slot, kind in slots)
    front = f"name: {name}\ndescription: Does {name} things.\n"
    if creds:
        front += f"credentials:\n{creds}"
    data = f"---\n{front}---\n\n{body}\n".encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{name}/SKILL.md")
        info.size, info.mode = len(data), 0o644
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _publish(client, headers, archive: bytes, meta: dict | None = None):
    return client.post(
        PUBLISH,
        headers=headers,
        files={"archive": ("skill.tgz", archive, "application/gzip")},
        data={"meta": json.dumps(meta or {})},
    )


def _ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def _named(*pairs: tuple[str, str]) -> list[dict]:
    """``publisher_credentials`` entries from ``(slot, credential_id)``."""
    return [{"slot": slot, "credential_id": cid} for slot, cid in pairs]


def _secret() -> str:
    return f"c5-secret-{uuid.uuid4().hex}"


def _cred(
    client, headers, slot: str, *, kind: str = "api_token", local: bool = False,
    secret: str | None = None, **fields,
) -> dict:
    """A credential of ``headers``' user carrying ``slot`` as ``service_uri``."""
    if kind == "api_token":
        data = {"api_token_type": "bearer", "api_token": secret or _secret()}
    else:  # an OAuth type: not locally compatible
        data = {
            "access_token": secret or _secret(),
            "refresh_token": "1//c5-refresh",
            "token_type": "Bearer",
            "expires_at": 9999999999,
            "scope": "https://www.googleapis.com/auth/gmail.modify",
        }
    cred = create_credential_via_api(client, headers, kind, data, service_uri=slot, **fields)
    if local:
        update_credential(client, headers, cred["id"], allow_local_use=True)
    return cred


def _provision_raw(client, headers, pkg: str, n: int = 1):
    return client.post(
        f"{API}/skills/packages/{pkg}/revisions/{n}/provision-local", headers=headers
    )


def _provision(client, headers, pkg: str, n: int = 1, *, secrets: tuple[str, ...] = ()) -> dict:
    """Provision and return ``{slot: row}``; asserts the response carries no data."""
    r = _provision_raw(client, headers, pkg, n)
    assert r.status_code == 200, r.text
    for secret in secrets:
        assert secret not in r.text
    body = r.json()
    assert set(body) == {"slots"}
    for row in body["slots"]:
        assert set(row) == SLOT_KEYS, row
    return {row["slot"]: row for row in body["slots"]}


def _outcomes(slots: dict) -> dict[str, tuple[str, str | None]]:
    return {slot: (row["outcome"], row["credential_id"]) for slot, row in slots.items()}


def _listed(client, desktop_headers) -> dict[str, dict]:
    r = list_desktop_credentials(client, desktop_headers)
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


def _refused(result: dict) -> dict[str, str]:
    return {r["id"]: r["reason"] for r in result["refused"]}


def _shared_with_me(client, headers) -> dict[str, dict]:
    r = client.get(f"{API}/credentials/shared-with-me", headers=headers)
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()["data"]}


def _shares_of(client, owner_headers, cid: str) -> list[dict]:
    r = client.get(f"{API}/credentials/{cid}/shares", headers=owner_headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _not_found(response, code: str) -> None:
    assert response.status_code == 404, response.text
    assert error_code(response.json()) == code, response.text


def _for_users(*grants: dict) -> dict:
    return {"visibility": "users", "grants": list(grants)}


# ── Scenario 1: publish freezes publisher / template / user ────────────────


def test_upload_publish_freezes_publisher_template_and_user_specs_from_named_credentials(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Preview without choices: every slot ``user``; the publisher's matching
       credentials are offered as candidates (publisher / template)
    2. Preview naming credentials agrees with the publish
    3. Publish: the shareable credential → ``publisher`` (its id frozen), the
       template-only one → ``template``, an unlisted matching shareable one
       and a slot with no credential → ``user``; no secret in the response
    4. The package detail shows the same specs
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    secret = _secret()
    shared = _cred(client, pub.headers, "svc-pub", allow_sharing=True, secret=secret)
    template = _cred(client, pub.headers, "svc-tpl", allow_template_sharing=True)
    update_credential(
        client, pub.headers, template["id"],
        template_private_fields=["api_token", "http_header_value"],
    )
    _cred(client, pub.headers, "svc-unlisted", allow_sharing=True)
    slots = [
        ("svc-pub", "api_token"), ("svc-tpl", "api_token"),
        ("svc-unlisted", "api_token"), ("svc-none", "api_token"),
    ]
    frontmatter = {
        "name": "freeze-tool",
        "credentials": [{"slot": s, "type": t} for s, t in slots],
    }
    choices = _named(("svc-pub", shared["id"]), ("svc-tpl", template["id"]))

    # ── Phase 1: preview without choices ─────────────────────────────
    plain = _ok(client.post(
        PREVIEW, headers=pub.headers, json={"name": "freeze-tool", "frontmatter": frontmatter}
    ))
    by_slot = {c["slot"]: c for c in plain["credentials"]}
    assert {s: c["provided_by"] for s, c in by_slot.items()} == {
        "svc-pub": "user", "svc-tpl": "user", "svc-unlisted": "user", "svc-none": "user",
    }
    assert secret not in json.dumps(plain)
    pub_candidates = {c["id"]: c for c in by_slot["svc-pub"]["candidates"]}
    assert "publisher" in pub_candidates[shared["id"]]["can_provide_as"]
    tpl_candidates = {c["id"]: c for c in by_slot["svc-tpl"]["candidates"]}
    assert tpl_candidates[template["id"]]["can_provide_as"] == ["template"]
    assert by_slot["svc-none"]["candidates"] == []

    # ── Phase 2: preview with choices ────────────────────────────────
    chosen = _ok(client.post(PREVIEW, headers=pub.headers, json={
        "name": "freeze-tool", "frontmatter": frontmatter, "publisher_credentials": choices,
    }))
    assert {c["slot"]: c["provided_by"] for c in chosen["credentials"]} == {
        "svc-pub": "publisher", "svc-tpl": "template",
        "svc-unlisted": "user", "svc-none": "user",
    }

    # ── Phase 3: publish ─────────────────────────────────────────────
    r = _publish(
        client, pub.headers, _skill_tgz("freeze-tool", slots),
        {"publisher_credentials": choices},
    )
    rev = _ok(r)
    assert secret not in r.text
    specs = {c["slot"]: c for c in rev["required_credentials"]}
    assert {s: c["provided_by"] for s, c in specs.items()} == {
        "svc-pub": "publisher", "svc-tpl": "template",
        "svc-unlisted": "user", "svc-none": "user",
    }
    assert specs["svc-pub"]["publisher_credential_id"] == shared["id"]
    assert specs["svc-unlisted"]["publisher_credential_id"] is None
    assert specs["svc-none"]["publisher_credential_id"] is None

    # ── Phase 4: the package detail agrees ───────────────────────────
    detail = _ok(client.get(f"{API}/skills/packages/{rev['package_id']}", headers=pub.headers))
    assert {
        c["slot"]: c["provided_by"] for c in detail["revisions"][0]["required_credentials"]
    } == {s: c["provided_by"] for s, c in specs.items()}


def test_upload_publish_refuses_a_named_credential_that_cannot_provide_the_slot(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Naming a credential that is not shareable, belongs to someone else,
    carries another slot, or does not exist → 422
    ``invalid_publisher_credential``; no package is created.
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    other = make_developer(client, su)
    private = _cred(client, pub.headers, "svc-a")
    foreign = _cred(client, other.headers, "svc-a", allow_sharing=True)
    elsewhere = _cred(client, pub.headers, "svc-b", allow_sharing=True)
    archive = _skill_tgz("refuse-tool", [("svc-a", "api_token")])

    for cid in (private["id"], foreign["id"], elsewhere["id"], str(uuid.uuid4())):
        r = _publish(client, pub.headers, archive, {"publisher_credentials": _named(("svc-a", cid))})
        assert r.status_code == 422, r.text
        assert error_code(r.json()) == "invalid_publisher_credential", r.text

    # nothing was published: a clean publish is still a first publish
    rev = _ok(_publish(client, pub.headers, archive))
    assert rev["revision_number"] == 1


# ── Scenario 2: the outcomes matrix ────────────────────────────────────────


def test_provision_local_outcomes_for_grantee_publisher_and_stranger(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Publisher names three shareable credentials (local use on / off, an
       OAuth type) and a template one; one slot is installer-filled
    2. ``use`` grantee: shared ×2 (with id; the OAuth type is locally
       compatible) / not_allowed_locally ×1 /
       user_provides ×2; ids only on shared; no secrets
    3. Second call: shared → already_shared, same id; exactly one share
       row for the grantee, source ``skill_local_install``
    4. The publisher: every publisher slot already_shared with its id
    5. Stranger → 404 package_not_found; revision 99 → 404
       revision_not_found; anonymous → 401
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    grantee = make_person(client, su)
    stranger = make_person(client, su)
    secrets = tuple(_secret() for _ in range(3))
    local = _cred(client, pub.headers, "svc-local", allow_sharing=True, local=True, secret=secrets[0])
    no_local = _cred(client, pub.headers, "svc-no-local", allow_sharing=True, secret=secrets[1])
    oauth = _cred(
        client, pub.headers, "svc-oauth", kind="gmail_oauth",
        allow_sharing=True, local=True, secret=secrets[2],
    )
    template = _cred(client, pub.headers, "svc-tpl", allow_template_sharing=True)
    update_credential(
        client, pub.headers, template["id"],
        template_private_fields=["api_token", "http_header_value"],
    )
    rev = _ok(_publish(
        client, pub.headers,
        _skill_tgz("matrix-tool", [
            ("svc-local", "api_token"), ("svc-no-local", "api_token"),
            ("svc-oauth", "gmail_oauth"), ("svc-tpl", "api_token"),
            ("svc-user", "api_token"),
        ]),
        {
            **_for_users(user_grant(grantee.id, "use")),
            "publisher_credentials": _named(
                ("svc-local", local["id"]), ("svc-no-local", no_local["id"]),
                ("svc-oauth", oauth["id"]), ("svc-tpl", template["id"]),
            ),
        },
    ))
    pkg = rev["package_id"]

    # ── Phase 2: the grantee ─────────────────────────────────────────
    first = _provision(client, grantee.headers, pkg, secrets=secrets)
    assert _outcomes(first) == {
        "svc-local": ("shared", local["id"]),
        "svc-no-local": ("not_allowed_locally", None),
        "svc-oauth": ("shared", oauth["id"]),
        "svc-tpl": ("user_provides", None),
        "svc-user": ("user_provides", None),
    }
    assert {s: r["provided_by"] for s, r in first.items()} == {
        "svc-local": "publisher", "svc-no-local": "publisher", "svc-oauth": "publisher",
        "svc-tpl": "template", "svc-user": "user",
    }
    assert first["svc-oauth"]["type"] == "gmail_oauth"

    # ── Phase 3: idempotent ──────────────────────────────────────────
    second = _provision(client, grantee.headers, pkg, secrets=secrets)
    assert _outcomes(second) == {
        **_outcomes(first),
        "svc-local": ("already_shared", local["id"]),
        "svc-oauth": ("already_shared", oauth["id"]),
    }
    rows = [s for s in _shares_of(client, pub.headers, local["id"])
            if s["shared_with_user_id"] == grantee.id]
    assert len(rows) == 1
    assert rows[0]["source"] == "skill_local_install"
    assert _shares_of(client, pub.headers, no_local["id"]) == []
    oauth_rows = [s for s in _shares_of(client, pub.headers, oauth["id"])
                  if s["shared_with_user_id"] == grantee.id]
    assert len(oauth_rows) == 1 and oauth_rows[0]["source"] == "skill_local_install"

    # ── Phase 4: the publisher holds them already ────────────────────
    own = _provision(client, pub.headers, pkg, secrets=secrets)
    assert _outcomes(own) == {
        "svc-local": ("already_shared", local["id"]),
        "svc-no-local": ("already_shared", no_local["id"]),
        "svc-oauth": ("already_shared", oauth["id"]),
        "svc-tpl": ("user_provides", None),
        "svc-user": ("user_provides", None),
    }

    # ── Phase 5: refusals ────────────────────────────────────────────
    _not_found(_provision_raw(client, stranger.headers, pkg), "package_not_found")
    _not_found(_provision_raw(client, grantee.headers, pkg, 99), "revision_not_found")
    _not_found(_provision_raw(client, grantee.headers, str(uuid.uuid4())), "package_not_found")
    assert _provision_raw(client, {}, pkg).status_code == 401
    assert not any(
        s["shared_with_user_id"] == stranger.id
        for s in _shares_of(client, pub.headers, local["id"])
    )


# ── Scenario 3: the share reaches the Desktop ──────────────────────────────


def test_provisioned_credential_reaches_the_recipients_desktop_and_no_one_else(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Before provisioning, the grantee's Desktop does not list it
    2. After: listed (category automatic, relation shared, local use
       allowed) and materialized; web list shows it "automatic" with source
       ``skill_local_install``
    3. A stranger's Desktop neither lists nor receives it
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    grantee = make_person(client, su)
    stranger = make_person(client, su)
    secret = _secret()
    cred = _cred(client, pub.headers, "svc-desk", allow_sharing=True, local=True, secret=secret)
    pkg = _ok(_publish(
        client, pub.headers, _skill_tgz("desk-tool", [("svc-desk", "api_token")]),
        {**_for_users(user_grant(grantee.id, "use")),
         "publisher_credentials": _named(("svc-desk", cred["id"]))},
    ))["package_id"]
    gd, _ = desktop_session(client, grantee.headers)
    sd, _ = desktop_session(client, stranger.headers)

    # ── Phase 1 ───────────────────────────────────────────────────────
    assert cred["id"] not in _listed(client, gd)
    assert _refused(materialize_desktop_credentials(client, gd, [cred["id"]])) == {
        cred["id"]: "no_access"
    }

    # ── Phase 2 ───────────────────────────────────────────────────────
    slots = _provision(client, grantee.headers, pkg, secrets=(secret,))
    assert _outcomes(slots) == {"svc-desk": ("shared", cred["id"])}
    item = _listed(client, gd)[cred["id"]]
    assert item["category"] == "automatic"
    assert item["relation"] == "shared"
    assert item["local_use_allowed"] is True
    assert item["service_uri"] == "svc-desk"
    result = materialize_desktop_credentials(client, gd, [cred["id"]])
    assert result["refused"] == []
    assert [i["id"] for i in result["items"]] == [cred["id"]]
    web = _shared_with_me(client, grantee.headers)[cred["id"]]
    assert web["category"] == "automatic"
    assert web["source"] == "skill_local_install"

    # ── Phase 3 ───────────────────────────────────────────────────────
    assert cred["id"] not in _listed(client, sd)
    assert _refused(materialize_desktop_credentials(client, sd, [cred["id"]])) == {
        cred["id"]: "no_access"
    }
    assert cred["id"] not in _shared_with_me(client, stranger.headers)


# ── Scenario 4: consent changes after publish ──────────────────────────────


def test_consent_changes_after_publish_are_rechecked_on_every_call(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Four publisher slots, all shared with the grantee
    2. Local use withdrawn → not_allowed_locally (no id); the Desktop is
       refused local_use_not_allowed; a new grantee gets no share
    3. Sharing turned off → not_shareable; slot moved → not_shareable;
       credential deleted → not_shareable
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    grantee = make_person(client, su)
    late = make_person(client, su)
    names = ("svc-a", "svc-b", "svc-c", "svc-d")
    creds = {n: _cred(client, pub.headers, n, allow_sharing=True, local=True) for n in names}
    pkg = _ok(_publish(
        client, pub.headers, _skill_tgz("consent-tool", [(n, "api_token") for n in names]),
        {**_for_users(user_grant(grantee.id, "use"), user_grant(late.id, "use")),
         "publisher_credentials": _named(*((n, creds[n]["id"]) for n in names))},
    ))["package_id"]

    # ── Phase 1 ───────────────────────────────────────────────────────
    assert {s: o for s, (o, _) in _outcomes(_provision(client, grantee.headers, pkg)).items()} == {
        n: "shared" for n in names
    }

    # ── Phase 2: local use withdrawn ─────────────────────────────────
    update_credential(client, pub.headers, creds["svc-a"]["id"], allow_local_use=False)
    # ── Phase 3: sharing off, slot moved, deleted ────────────────────
    set_credential_sharing(client, pub.headers, creds["svc-b"]["id"], False)
    update_credential(client, pub.headers, creds["svc-c"]["id"], service_uri="svc-moved")
    r = client.delete(
        f"{API}/credentials/{creds['svc-d']['id']}", headers=pub.headers, params={"force": True}
    )
    assert r.status_code == 200, r.text

    expected = {
        "svc-a": ("not_allowed_locally", None),
        "svc-b": ("not_shareable", None),
        "svc-c": ("not_shareable", None),
        "svc-d": ("not_shareable", None),
    }
    assert _outcomes(_provision(client, grantee.headers, pkg)) == expected
    assert _outcomes(_provision(client, late.headers, pkg)) == expected
    assert not any(
        s["shared_with_user_id"] == late.id
        for n in ("svc-a", "svc-c")
        for s in _shares_of(client, pub.headers, creds[n]["id"])
    )

    gd, _ = desktop_session(client, grantee.headers)
    assert _refused(materialize_desktop_credentials(client, gd, [creds["svc-a"]["id"]])) == {
        creds["svc-a"]["id"]: "local_use_not_allowed"
    }


# ── Scenario 5: U7 — through a desktop agent pinning the revision ─────────


class _Share:
    """A clone of a desktop agent's repo; each ``push`` is one agent version."""

    def __init__(self, bridge, tmp_path, owner, item) -> None:
        self.item = item
        self.git = GitClient(tmp_path, token=owner.headers["Authorization"].removeprefix("Bearer "))
        self.work = tmp_path / f"work-{uuid.uuid4().hex[:6]}"
        assert self.git.clone(bridge.repo_url(repo_id_of(item)), self.work).ok
        self.n = 0

    def push(self, catalog_skills: list[dict]) -> None:
        major, minor, patch = server_contract_version()
        manifest = {
            "id": manifest_id_of(self.item),
            "name": "Pinning Agent",
            "slug": self.item["name"],
            "description": "Pins catalog skills",
            "contract_version": f"{major}.{minor}.{patch}",
            "catalog_skills": catalog_skills,
        }
        write_agent_tree(
            self.work, slug=self.item["name"], manifest=manifest,
            manifest_id=manifest_id_of(self.item),
        )
        self.n += 1
        (self.work / f"step-{self.n}.md").write_text(str(self.n))
        self.git.commit_all(self.work, f"version {self.n}")
        result = self.git.push(self.work)
        assert result.ok, result.output


def test_u7_caller_provisions_through_a_desktop_agent_pinning_the_revision(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """
    1. A private skill with a publisher slot; C has no grant on it → 404
    2. A desktop agent C may use pins revision 1 → C provisions (shared) and
       the credential reaches C's Desktop
    3. Revision 2 (not pinned) → 404 package_not_found for C
    """
    su = superuser_token_headers
    a = make_developer(client, su)
    c = make_person(client, su)
    cred = _cred(client, a.headers, "svc-u7", allow_sharing=True, local=True)
    rev1 = _ok(_publish(
        client, a.headers, _skill_tgz("u7-tool", [("svc-u7", "api_token")]),
        {"publisher_credentials": _named(("svc-u7", cred["id"]))},
    ))
    pkg = rev1["package_id"]
    skill_item = get_item(client, a.headers, pkg)
    assert skill_item["visibility"] == "private"

    # ── Phase 1 ───────────────────────────────────────────────────────
    _not_found(_provision_raw(client, c.headers, pkg), "package_not_found")

    # ── Phase 2 ───────────────────────────────────────────────────────
    agent = create_desktop_agent(
        client, a.headers, visibility="users", grants=[user_grant(c.id, "use")]
    )
    share = _Share(bridge, tmp_path, a, agent)
    share.push([{
        "package_id": skill_item["id"], "package_uuid": pkg, "name": "u7-tool",
        "revision_number": 1, "archive_sha256": skill_item["skill"]["archive_sha256"],
    }])
    assert _outcomes(_provision(client, c.headers, pkg)) == {"svc-u7": ("shared", cred["id"])}
    cd, _ = desktop_session(client, c.headers)
    assert _listed(client, cd)[cred["id"]]["category"] == "automatic"
    assert client.get(f"{API}/catalog/items/{pkg}", headers=c.headers).status_code == 404

    # ── Phase 3 ───────────────────────────────────────────────────────
    rev2 = _ok(_publish(
        client, a.headers, _skill_tgz("u7-tool", [("svc-u7", "api_token")], body="Two."),
        {"package_uuid": pkg, "publisher_credentials": _named(("svc-u7", cred["id"]))},
    ))
    assert rev2["revision_number"] == 2
    _not_found(_provision_raw(client, c.headers, pkg, 2), "package_not_found")


# ── Edge cases (rules a reviewer may adjust) ───────────────────────────────


def test_edge_pre_existing_direct_share_is_already_shared_and_keeps_its_source(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A grantee who already holds a direct share → already_shared with the
    id; the share keeps its ``direct`` source (category "mine")."""
    su = superuser_token_headers
    pub = make_developer(client, su)
    grantee = make_person(client, su)
    cred = _cred(client, pub.headers, "svc-direct", allow_sharing=True, local=True)
    share_credential_via_api(client, pub.headers, cred["id"], grantee.email)
    pkg = _ok(_publish(
        client, pub.headers, _skill_tgz("direct-tool", [("svc-direct", "api_token")]),
        {**_for_users(user_grant(grantee.id, "use")),
         "publisher_credentials": _named(("svc-direct", cred["id"]))},
    ))["package_id"]

    assert _outcomes(_provision(client, grantee.headers, pkg)) == {
        "svc-direct": ("already_shared", cred["id"])
    }
    rows = [s for s in _shares_of(client, pub.headers, cred["id"])
            if s["shared_with_user_id"] == grantee.id]
    assert len(rows) == 1
    assert rows[0]["source"] in (None, "direct")
    assert _shared_with_me(client, grantee.headers)[cred["id"]]["category"] == "mine"


def test_edge_pre_existing_skill_install_share_is_promoted_to_local_install(
    client: TestClient, superuser_token_headers: dict[str, str], patch_environment_adapter
) -> None:
    """
    1. The installer holds the publisher's credential through a cloud skill
       install (source ``skill_install``) — not deliverable to a Desktop
    2. provision-local on a desktop-uploaded skill naming the same
       credential → already_shared with id; the share becomes
       ``skill_local_install`` and the Desktop lists and receives it
    """
    su = superuser_token_headers
    pub = make_developer(client, su)
    installer = make_developer(client, su)
    cred = _cred(client, pub.headers, "svc-cloud", allow_sharing=True, local=True)

    # ── Phase 1: cloud skill install ─────────────────────────────────
    pub_agent, pub_env = make_agent_with_env(client, pub.headers, "C5-Cloud-Publisher")
    link_credential_to_agent(client, pub.headers, pub_agent, cred["id"])
    write_skill_with_credentials(pub_env, "cloud-c5", [{"slot": "svc-cloud", "type": "api_token"}])
    cloud = publish_skill(client, pub.headers, pub_agent, "cloud-c5", visibility="public")
    con_agent, _ = make_agent_with_env(client, installer.headers, "C5-Cloud-Consumer")
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = {"hash": "hash-empty", "skills": [], "errors": []}
    adapter.workspace_files = {}
    patch_environment_adapter.get_adapter = lambda env: adapter
    install_skill(client, installer.headers, con_agent, cloud["package_id"])
    assert _shared_with_me(client, installer.headers)[cred["id"]]["source"] == "skill_install"
    idh, _ = desktop_session(client, installer.headers)
    assert cred["id"] not in _listed(client, idh)

    # ── Phase 2: provision-local promotes the share ──────────────────
    pkg = _ok(_publish(
        client, pub.headers, _skill_tgz("local-c5", [("svc-cloud", "api_token")]),
        {**_for_users(user_grant(installer.id, "use")),
         "publisher_credentials": _named(("svc-cloud", cred["id"]))},
    ))["package_id"]
    assert _outcomes(_provision(client, installer.headers, pkg)) == {
        "svc-cloud": ("already_shared", cred["id"])
    }
    assert _shared_with_me(client, installer.headers)[cred["id"]]["source"] == "skill_local_install"
    assert _listed(client, idh)[cred["id"]]["category"] == "automatic"
    result = materialize_desktop_credentials(client, idh, [cred["id"]])
    assert [i["id"] for i in result["items"]] == [cred["id"]]


def test_edge_pre_existing_bundle_install_share_is_not_allowed_locally(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The installer holds the credential through a bundle install (source
    ``bundle_install``) → not_allowed_locally, no id; the share keeps its
    source and stays off the Desktop."""
    su = superuser_token_headers
    publisher_agent = create_agent_via_api(client, su, name="C5 Bundle Publisher")
    drain_tasks()
    cred = create_bundle_credential(client, su, name="c5-bundle-cred", allow_sharing=True)
    update_credential(client, su, cred["id"], allow_local_use=True, service_uri="svc-bundle")
    link_bundle_credential_to_agent(client, su, publisher_agent["id"], cred["id"])
    fresh = publish_bundle(client, su, publisher_agent["id"])
    make_bundle_public(client, su, fresh["bundle_uuid"])
    installer = make_developer(client, su)  # needs a default AI credential
    install_bundle(client, installer.headers, fresh["bundle_id"])
    assert _shared_with_me(client, installer.headers)[cred["id"]]["source"] == "bundle_install"

    pkg = _ok(_publish(
        client, su, _skill_tgz(f"bundle-c5-{uuid.uuid4().hex[:6]}", [("svc-bundle", "api_token")]),
        {**_for_users(user_grant(installer.id, "use")),
         "publisher_credentials": _named(("svc-bundle", cred["id"]))},
    ))["package_id"]
    assert _outcomes(_provision(client, installer.headers, pkg)) == {
        "svc-bundle": ("not_allowed_locally", None)
    }
    assert _shared_with_me(client, installer.headers)[cred["id"]]["source"] == "bundle_install"
    idh, _ = desktop_session(client, installer.headers)
    assert cred["id"] not in _listed(client, idh)


def test_edge_editor_republish_naming_the_editors_own_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """An ``edit`` grantee may republish, but not provide credentials: naming
    their own shareable credential → 422 ``invalid_publisher_credential`` and
    no revision; the same republish without ``publisher_credentials`` works
    and the slot stays user-provided."""
    su = superuser_token_headers
    pub = make_developer(client, su)
    editor = make_person(client, su)
    grantee = make_person(client, su)
    archive = _skill_tgz("edited-tool", [("svc-ed", "api_token")])
    pkg = _ok(_publish(
        client, pub.headers, archive,
        _for_users(user_grant(editor.id, "edit"), user_grant(grantee.id, "use")),
    ))["package_id"]
    cred = _cred(client, editor.headers, "svc-ed", allow_sharing=True, local=True)

    refused = _publish(
        client, editor.headers, archive,
        {"package_uuid": pkg, "publisher_credentials": _named(("svc-ed", cred["id"]))},
    )
    assert refused.status_code == 422, refused.text
    assert error_code(refused.json()) == "invalid_publisher_credential", refused.text

    rev2 = _ok(_publish(client, editor.headers, archive, {"package_uuid": pkg}))
    assert rev2["revision_number"] == 2
    spec = rev2["required_credentials"][0]
    assert (spec["provided_by"], spec["publisher_credential_id"]) == ("user", None)

    assert _outcomes(_provision(client, grantee.headers, pkg, 2)) == {
        "svc-ed": ("user_provides", None)
    }
    assert _shares_of(client, editor.headers, cred["id"]) == []
