"""Skills catalog (Phase 3) — publishing, browsing, and managing a package.

Scenarios:
  1. Publish creates package + revision atomically; a second publish appends
     revision 2 and moves the package's pointer, while revision 1 keeps serving
     the bytes it was published with. Package identity (``package_id``) is
     immutable once published.
  2. Every refusal fires BEFORE anything is written: no environment, a
     workspace that was never materialised, a skill that is not there, a skill
     that does not validate, and a skill carrying key material. The catalog is
     still empty afterwards.
  3. Publishing from a SUSPENDED environment succeeds — the files are read from
     the host workspace, and nothing wakes the container. This is a decision,
     not an accident, so it is pinned.
  4. The ``user_can_see`` matrix across private → public → delisted, for the
     publisher, a stranger and an administrator, on list / detail / content —
     including the delist-then-view case that keeps ``delist`` from being a
     trapdoor for the admin who performed it.
  5. Publisher-editable metadata: the catalog blurb survives a re-publish once
     it has been edited, and the manage verbs are publisher-only while
     ``delist`` is superuser-only.
  6. ``visibility='users'``: a grant is what makes the package visible, a
     mixed-case address still finds the account, and the grant routes' four
     refusals (``self_grant`` / ``user_not_found`` / ``grant_not_found`` /
     ``not_publisher``). Re-publishing and changing visibility both KEEP the
     grants.
  7. Publishing with ``grant_emails``: the grants and the revision land
     together, they are additive across re-publishes, the publisher's own
     address is skipped rather than refused, and one bad address fails the
     whole publish without leaving a half-published package behind.
  8. ``grant_emails`` on a publish whose EFFECTIVE visibility is not ``users``
     is refused (409 ``grants_require_users_visibility``) before anything is
     written — including the omitted-visibility shape, where the effective
     value comes from the package that already exists. The standalone grant
     route stays permissive: granting ahead of flipping the visibility is
     legitimate preparation.

Test seam:
  Publishing reads the host-side workspace
  (``ENV_INSTANCES_DIR/<env id>/app/workspace``), seeded through
  ``tests/utils/skill_catalog.write_skill`` — the same seam the bundle
  workspace-snapshot tests use. Revision snapshots and the archive cache are
  redirected to a tmp tree by the module's ``skill_storage`` fixture.

Notes:
  Archive determinism and the container's safe-extract are unit-tested in
  ``tests/unit/test_skill_catalog_archive.py``; the container-side install of a
  published archive is in ``tests/unit/test_skill_catalog_container_install.py``.
  Install / upgrade / uninstall / the archive route live in
  ``agents_skills_catalog_install_test.py``.
"""
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.environment import (
    delete_environment,
    set_environment_status,
)
from tests.utils.skill_catalog import (
    add_skill_package_grant,
    catalog_ids,
    delist_skill_package,
    entry_for,
    error_code,
    get_revision_content,
    get_skill_package,
    grant_emails_of,
    list_skill_catalog,
    list_skill_package_grants,
    make_agent_with_env,
    make_developer,
    mixed_case,
    patched_skill_storage,
    publish_skill,
    revoke_skill_package_grant,
    update_skill_package,
    workspace_root,
    write_skill,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    """Redirect ``SKILL_STORAGE_DIR`` at a tmp tree for every test here."""
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


# ── Scenario 1: publish lifecycle ──────────────────────────────────────────


def test_publish_creates_a_package_then_appends_immutable_revisions(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Publishing one skill twice:
      1. Seed ``skills/pdf-report/`` with a script and a reference file.
      2. Publish → revision 1, and a package that did not exist before.
      3. The catalog lists it for its publisher with the projected fields.
      4. Detail + content read back the published revision.
      5. Edit the skill and publish again → revision 2; the package points at
         it, the detail lists both newest-first, and revision 1 still serves
         its ORIGINAL bytes.
      6. A publish naming a different ``package_id`` is refused (409), not
         silently ignored — installs reference that id.
      7. An unknown revision number is a 404, with a code.
    """
    # ── Phase 1: publisher workspace ─────────────────────────────────────
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Skill-Publisher")
    write_skill(
        env_id,
        "pdf-report",
        description="Turns a report into a PDF.",
        body="Original body.",
        extra={
            "scripts/run.sh": "#!/bin/sh\necho hi\n",
            "references/spec.md": "the spec\n",
        },
    )

    # ── Phase 2: first publish → package + revision, atomically ──────────
    revision = publish_skill(
        client,
        headers,
        agent_id,
        "pdf-report",
        version="1.0",
        release_notes="First cut",
    )
    assert revision["revision_number"] == 1
    assert revision["version"] == "1.0"
    assert revision["release_notes"] == "First cut"
    assert revision["frontmatter"]["name"] == "pdf-report"
    assert revision["frontmatter"]["description"] == "Turns a report into a PDF."
    assert len(revision["content_hash"]) == 64
    assert revision["size_bytes"] > 0
    assert revision["published_by_user_id"] == _user["id"]

    # ── Phase 3: the catalog row the publisher sees ──────────────────────
    entries = list_skill_catalog(client, headers)
    assert len(entries) == 1, entries
    entry = entries[0]
    package_uuid = entry["id"]
    assert entry["name"] == "pdf-report"
    assert entry["display_name"] == "pdf-report"
    assert entry["description"] == "Turns a report into a PDF."
    assert entry["visibility"] == "private"  # default, not "public"
    assert entry["is_listed"] is True
    assert entry["can_manage"] is True
    assert entry["publisher_user_id"] == _user["id"]
    assert entry["source_agent_id"] == agent_id
    assert entry["latest_revision_id"] == revision["id"]
    assert entry["latest_revision_number"] == 1
    assert entry["latest_version"] == "1.0"
    assert entry["latest_revision"]["id"] == revision["id"]
    # Nobody has installed it, and the publisher is not "somebody".
    assert entry["install_count"] == 0
    assert entry["installed_in_agent_ids"] == []
    # Default identity: <reversed host>.<8 hex of publisher>.<skill name>
    assert entry["package_id"].endswith(f".{_user['id'][:8]}.pdf-report"), (
        entry["package_id"]
    )

    # ── Phase 4: detail + content ────────────────────────────────────────
    detail = get_skill_package(client, headers, package_uuid)
    assert [r["revision_number"] for r in detail["revisions"]] == [1]
    content = get_revision_content(client, headers, package_uuid, 1)
    assert content["name"] == "pdf-report"
    assert content["truncated"] is False
    assert "Original body." in content["content"]
    assert "name: pdf-report" in content["content"]

    # ── Phase 5: second publish → revision 2; revision 1 is frozen ───────
    write_skill(
        env_id,
        "pdf-report",
        description="Turns a report into a PDF.",
        body="Rewritten body.",
        extra={"scripts/run.sh": "#!/bin/sh\necho hi\n"},
    )
    revision2 = publish_skill(
        client, headers, agent_id, "pdf-report", version="2.0"
    )
    assert revision2["revision_number"] == 2
    assert revision2["id"] != revision["id"]
    assert revision2["content_hash"] != revision["content_hash"]

    detail = get_skill_package(client, headers, package_uuid)
    assert [r["revision_number"] for r in detail["revisions"]] == [2, 1]
    assert detail["latest_revision_number"] == 2
    assert detail["latest_version"] == "2.0"

    assert "Rewritten body." in get_revision_content(
        client, headers, package_uuid, 2
    )["content"]
    # The whole point of an immutable revision: an install pinned to 1 still
    # gets what it was pinned to.
    assert "Original body." in get_revision_content(
        client, headers, package_uuid, 1
    )["content"]

    # ── Phase 6: package_id is immutable once published ──────────────────
    refused = publish_skill(
        client,
        headers,
        agent_id,
        "pdf-report",
        package_id="io.example.someone-else.pdf-report",
        expected_status=409,
    )
    assert error_code(refused) == "package_id_immutable"
    # No third revision was written.
    assert len(get_skill_package(client, headers, package_uuid)["revisions"]) == 2

    # ── Phase 7: unknown revision ────────────────────────────────────────
    missing = get_revision_content(
        client, headers, package_uuid, 99, expected_status=404
    )
    assert error_code(missing) == "revision_not_found"


def test_a_supplied_package_id_is_validated_and_claimed_once(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    First publish with an explicit reverse-DNS id:
      1. A malformed id is refused (422) before the package exists.
      2. A well-formed one is used verbatim.
      3. Another publisher cannot claim the same id (409) — ids are
         instance-wide even though skill NAMES are per publisher.
    """
    _a, headers_a = make_developer(client, superuser_token_headers)
    agent_a, env_a = make_agent_with_env(client, headers_a, "Id-Publisher-A")
    write_skill(env_a, "shared-name")

    # ── Phase 1: malformed id ────────────────────────────────────────────
    bad = publish_skill(
        client,
        headers_a,
        agent_a,
        "shared-name",
        package_id="not a reverse dns id",
        expected_status=422,
    )
    assert error_code(bad) == "package_id_invalid"
    assert list_skill_catalog(client, headers_a) == []

    # ── Phase 2: a good one is kept verbatim ─────────────────────────────
    publish_skill(
        client,
        headers_a,
        agent_a,
        "shared-name",
        package_id="io.example.team.shared-name",
    )
    entry = list_skill_catalog(client, headers_a)[0]
    assert entry["package_id"] == "io.example.team.shared-name"

    # ── Phase 3: a second publisher may reuse the NAME, not the ID ───────
    _b, headers_b = make_developer(client, superuser_token_headers)
    agent_b, env_b = make_agent_with_env(client, headers_b, "Id-Publisher-B")
    write_skill(env_b, "shared-name")

    taken = publish_skill(
        client,
        headers_b,
        agent_b,
        "shared-name",
        package_id="io.example.team.shared-name",
        expected_status=409,
    )
    assert error_code(taken) == "package_id_taken"

    # The same skill name under B's own generated id is fine (§9).
    publish_skill(client, headers_b, agent_b, "shared-name")
    b_entry = list_skill_catalog(client, headers_b)[0]
    assert b_entry["name"] == "shared-name"
    assert b_entry["package_id"] != "io.example.team.shared-name"


# ── Scenario 2: refusals happen before anything is written ─────────────────


def test_publish_refusals_are_coded_and_write_nothing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Every publish pre-flight refusal, each with its own fix:
      1. An agent with no environment → 409 ``no_environment``.
      2. An environment whose workspace is not on disk → 409
         ``workspace_unavailable``.
      3. No such skill / an invalid skill name → 404 ``skill_not_found``.
      4. A skill that does not validate → 422 ``skill_invalid``.
      5. A skill carrying key material → 422 ``skill_contains_secrets``,
         naming the offending paths.
      6. A stranger publishing someone else's agent → 403.
      7. After all of that the catalog is still empty — a refusal is not a
         half-published package.
    """
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Refusal-Publisher")

    # ── Phase 1: workspace present but the skill is not ──────────────────
    missing = publish_skill(
        client, headers, agent_id, "ghost-skill", expected_status=404
    )
    assert error_code(missing) == "skill_not_found"

    # A name that cannot be a skill name never becomes a path segment.
    bad_name = publish_skill(
        client, headers, agent_id, "Bad_Name", expected_status=404
    )
    assert error_code(bad_name) == "skill_not_found"

    # ── Phase 2: invalid skill (SKILL.md name ≠ folder) ──────────────────
    write_skill(
        env_id,
        "mismatched",
        frontmatter="name: something-else\ndescription: Nope.",
    )
    invalid = publish_skill(
        client, headers, agent_id, "mismatched", expected_status=422
    )
    assert error_code(invalid) == "skill_invalid"

    # ── Phase 3: secrets inside the skill ────────────────────────────────
    write_skill(
        env_id,
        "leaky",
        extra={".env": "OPENAI_API_KEY=sk-verysecret\n"},
    )
    secrets = publish_skill(
        client, headers, agent_id, "leaky", expected_status=422
    )
    assert error_code(secrets) == "skill_contains_secrets"
    assert secrets["detail"]["paths"] == [".env"]

    # ── Phase 4: a stranger cannot publish this agent's skills ───────────
    # 404, not 403: the route's agent lookup is the shared
    # ``LLMPluginService.verify_agent_access``, which answers a non-owner
    # exactly as it answers an id that does not exist, so the response cannot
    # be used to discover that somebody else's agent is real. Same rule as
    # ``assert_can_build``'s ``not_accessible``, applied before it runs.
    write_skill(env_id, "fine-skill")
    _other, other_headers = make_developer(client, superuser_token_headers)
    r = client.post(
        f"{API}/agents/{agent_id}/skills/fine-skill/publish",
        headers=other_headers,
        json={},
    )
    assert r.status_code == 404, r.text

    # ── Phase 5: workspace never materialised → a different 409 ──────────
    shutil.rmtree(workspace_root(env_id))
    unavailable = publish_skill(
        client, headers, agent_id, "fine-skill", expected_status=409
    )
    assert error_code(unavailable) == "workspace_unavailable"

    # ── Phase 6: no environment at all → its own 409 ─────────────────────
    delete_environment(client, headers, env_id)
    drained = publish_skill(
        client, headers, agent_id, "fine-skill", expected_status=409
    )
    assert error_code(drained) == "no_environment"

    # ── Phase 7: nothing was written along the way ───────────────────────
    assert list_skill_catalog(client, headers) == []


# ── Scenario 3: a suspended environment publishes ──────────────────────────


def test_publish_from_a_suspended_environment_never_wakes_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A suspended environment publishes exactly like a running one:
      1. Seed the skill, then force the environment to ``suspended``.
      2. Publish → 200 with revision 1.
      3. The environment is still suspended afterwards — waking it would cost
         a container start and change nothing about the bytes read from disk.
    """
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Suspended-Publisher")
    write_skill(env_id, "cold-start", description="Publishes while asleep.")

    set_environment_status(db, env_id, "suspended")

    revision = publish_skill(client, headers, agent_id, "cold-start")
    assert revision["revision_number"] == 1

    r = client.get(f"{API}/environments/{env_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "suspended", (
        "publishing must not start the container"
    )


# ── Scenario 4: the visibility matrix, including delist-then-view ──────────


def test_visibility_matrix_across_private_public_and_delisted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Who may see a package, at each of its three states:

      | state              | publisher | stranger | superuser |
      |--------------------|-----------|----------|-----------|
      | private            | yes       | no       | no        |
      | public + listed    | yes       | yes      | yes       |
      | public + delisted  | yes       | no       | detail    |

      1. Publish privately → only the publisher sees it anywhere.
      2. Make it public → the stranger and the admin both see it, and the
         stranger's entry says ``can_manage: false``.
      3. Manage verbs: the stranger cannot PATCH (403) and cannot delist (403).
      4. The admin delists → it leaves the stranger's world entirely, stays in
         the publisher's list, and remains READABLE by the admin: otherwise
         delist would 404 its own author, a second delist would not be
         idempotent, and nobody could inspect a re-listed package.
      5. Delist is idempotent, and the publisher can put it back.
    """
    publisher, pub_headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, pub_headers, "Matrix-Publisher")
    write_skill(env_id, "matrix-skill", description="Shows who can see it.")
    revision = publish_skill(client, pub_headers, agent_id, "matrix-skill")

    package_uuid = list_skill_catalog(client, pub_headers)[0]["id"]
    _stranger, stranger_headers = create_random_user_with_headers(client)

    # ── Phase 1: private ─────────────────────────────────────────────────
    assert package_uuid in catalog_ids(list_skill_catalog(client, pub_headers))
    for headers in (stranger_headers, superuser_token_headers):
        assert package_uuid not in catalog_ids(list_skill_catalog(client, headers))
        assert (
            error_code(
                get_skill_package(client, headers, package_uuid, expected_status=404)
            )
            == "package_not_found"
        )
        get_revision_content(
            client, headers, package_uuid, 1, expected_status=404
        )

    # Unauthenticated callers get nothing at all.
    assert client.get(f"{API}/skills/catalog").status_code in (401, 403)
    assert client.get(
        f"{API}/skills/packages/{package_uuid}"
    ).status_code in (401, 403)

    # ── Phase 2: public + listed ─────────────────────────────────────────
    update_skill_package(
        client, pub_headers, package_uuid, visibility="public"
    )
    for headers in (stranger_headers, superuser_token_headers):
        entry = entry_for(list_skill_catalog(client, headers), package_uuid)
        assert entry is not None
        assert entry["can_manage"] is False
        detail = get_skill_package(client, headers, package_uuid)
        assert detail["latest_revision_id"] == revision["id"]
        content = get_revision_content(client, headers, package_uuid, 1)
        assert "matrix-skill" in content["content"]

    # ── Phase 3: manage verbs stay with their owners ─────────────────────
    forbidden = update_skill_package(
        client,
        stranger_headers,
        package_uuid,
        display_name="Hijacked",
        expected_status=403,
    )
    assert error_code(forbidden) == "not_publisher"
    not_admin = delist_skill_package(
        client, stranger_headers, package_uuid, expected_status=403
    )
    assert error_code(not_admin) == "not_superuser"

    # Validation on the publisher's own edits.
    assert (
        error_code(
            update_skill_package(
                client,
                pub_headers,
                package_uuid,
                visibility="everyone",
                expected_status=422,
            )
        )
        == "invalid_visibility"
    )
    assert (
        error_code(
            update_skill_package(
                client,
                pub_headers,
                package_uuid,
                display_name="   ",
                expected_status=422,
            )
        )
        == "invalid_display_name"
    )

    # ── Phase 4: delisted — the trapdoor case ────────────────────────────
    delisted = delist_skill_package(
        client, superuser_token_headers, package_uuid
    )
    assert delisted["is_listed"] is False

    # Stranger: gone from list, detail and content alike.
    assert package_uuid not in catalog_ids(
        list_skill_catalog(client, stranger_headers)
    )
    get_skill_package(client, stranger_headers, package_uuid, expected_status=404)
    get_revision_content(
        client, stranger_headers, package_uuid, 1, expected_status=404
    )

    # Admin: not listed, but still readable — delist must not 404 its author.
    assert package_uuid not in catalog_ids(
        list_skill_catalog(client, superuser_token_headers)
    )
    admin_view = get_skill_package(
        client, superuser_token_headers, package_uuid
    )
    assert admin_view["is_listed"] is False
    assert admin_view["can_manage"] is False
    get_revision_content(client, superuser_token_headers, package_uuid, 1)

    # Publisher: unchanged in every respect but the flag.
    pub_entry = entry_for(list_skill_catalog(client, pub_headers), package_uuid)
    assert pub_entry is not None
    assert pub_entry["is_listed"] is False
    assert pub_entry["can_manage"] is True

    # ── Phase 5: idempotent, and re-listable by its publisher ────────────
    assert (
        delist_skill_package(client, superuser_token_headers, package_uuid)[
            "is_listed"
        ]
        is False
    )
    update_skill_package(client, pub_headers, package_uuid, is_listed=True)
    assert package_uuid in catalog_ids(
        list_skill_catalog(client, stranger_headers)
    )
    assert publisher["id"] == pub_entry["publisher_user_id"]


# ── Scenario 5: publisher-edited metadata survives a re-publish ────────────


def test_an_edited_blurb_survives_the_next_publish(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The catalog description follows the skill's frontmatter until a human
    changes it:
      1. Publish → description mirrors the SKILL.md frontmatter.
      2. Change the frontmatter and re-publish → the mirror follows.
      3. Edit the catalog blurb by hand, then publish a third time → the hand
         written blurb survives. §5.3 says publish "updates description"; taken
         literally that would silently overwrite what someone typed.
      4. A hidden package is still fully editable by its publisher.
    """
    _user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, "Blurb-Publisher")

    write_skill(env_id, "blurb", description="First description.")
    publish_skill(client, headers, agent_id, "blurb")
    package_uuid = list_skill_catalog(client, headers)[0]["id"]
    assert (
        get_skill_package(client, headers, package_uuid)["description"]
        == "First description."
    )

    write_skill(env_id, "blurb", description="Second description.")
    publish_skill(client, headers, agent_id, "blurb")
    assert (
        get_skill_package(client, headers, package_uuid)["description"]
        == "Second description."
    )

    updated = update_skill_package(
        client,
        headers,
        package_uuid,
        display_name="Blurb Maker",
        description="A blurb a human wrote.",
    )
    assert updated["display_name"] == "Blurb Maker"

    write_skill(env_id, "blurb", description="Third description.")
    publish_skill(client, headers, agent_id, "blurb")
    final = get_skill_package(client, headers, package_uuid)
    assert final["description"] == "A blurb a human wrote."
    assert final["display_name"] == "Blurb Maker"
    assert final["latest_revision_number"] == 3
    # The revision keeps the frontmatter as published — only the package blurb
    # is editable.
    assert final["latest_revision"]["frontmatter"]["description"] == (
        "Third description."
    )


def test_unknown_package_ids_are_not_found(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A package uuid nobody published is a 404 on every read route.

    ``delist`` is the exception that proves the ordering rule: it 404s for an
    administrator and 403s for everybody else, so the refusal a non-admin sees
    carries no information about whether the package exists.
    """
    ghost = str(uuid.uuid4())
    assert (
        error_code(
            get_skill_package(
                client, superuser_token_headers, ghost, expected_status=404
            )
        )
        == "package_not_found"
    )
    get_revision_content(
        client, superuser_token_headers, ghost, 1, expected_status=404
    )
    delist_skill_package(
        client, superuser_token_headers, ghost, expected_status=404
    )

    # …but only for the one caller entitled to tell the difference. A
    # non-administrator is refused 403 BEFORE the package is looked up, so a
    # missing id and a real private package answer identically. Checking the
    # role after the load would answer 404 here and 403 for a package that
    # exists — an existence oracle over every private skill on the instance,
    # handed to any authenticated caller.
    _nobody, nobody_headers = create_random_user_with_headers(client)
    assert (
        error_code(
            delist_skill_package(
                client, nobody_headers, ghost, expected_status=403
            )
        )
        == "not_superuser"
    )


# ── Scenario 6: `users` visibility is an explicit allowlist ────────────────


def test_users_visibility_is_governed_by_grants_and_survives_republish(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A skill shared with named colleagues rather than with everybody:

      1. Published ``users`` with nobody named → nobody but the publisher sees
         it. That is allowed: the package is effectively private until a grant
         exists.
      2. Granted by a MIXED-CASE address → resolves to the account (stored
         lowercase). The colleague now sees it, flagged ``is_granted``; the
         publisher's own row is not.
      3. Nobody else gains anything: a stranger 404s, and an administrator gets
         no bypass — ``users`` is an allowlist and an admin is not on it.
      4. The grant routes' refusals: a duplicate is an idempotent 200, the
         publisher's own address is 409 ``self_grant``, an unknown address is
         404 ``user_not_found``, revoking somebody who has none is 404
         ``grant_not_found``, and a non-publisher who CAN see the package still
         gets 403 ``not_publisher`` on all three verbs.
      5. A granted package still respects ``is_listed``, whether the publisher
         unlists it or an administrator delists it — and the administrator can
         reach a ``users`` package they hold no grant on, which is the whole
         point of that verb.
      6. Re-publishing keeps the grants, and so does moving the visibility to
         ``public`` and back — revoking is its own verb, never a side effect.
      7. Revoking hides it: the colleague loses the catalog row and the detail
         route.
    """
    publisher, pub_headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, pub_headers, "Grants-Publisher")
    write_skill(env_id, "team-notes", description="Files the team note.")

    colleague, col_headers = create_random_user_with_headers(client)
    _stranger, stranger_headers = create_random_user_with_headers(client)

    # ── Phase 1: users-visibility with nobody named ──────────────────────
    publish_skill(
        client, pub_headers, agent_id, "team-notes", version="1.0",
        visibility="users",
    )
    package_uuid = list_skill_catalog(client, pub_headers)[0]["id"]
    assert get_skill_package(client, pub_headers, package_uuid)[
        "visibility"
    ] == "users"

    for headers in (col_headers, stranger_headers, superuser_token_headers):
        assert package_uuid not in catalog_ids(list_skill_catalog(client, headers))
        assert (
            error_code(
                get_skill_package(client, headers, package_uuid, expected_status=404)
            )
            == "package_not_found"
        )
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 0

    # ── Phase 2: a grant typed the way a colleague writes it ─────────────
    typed = mixed_case(colleague["email"])
    assert typed != colleague["email"], "the address must be genuinely mixed-case"
    grant = add_skill_package_grant(client, pub_headers, package_uuid, typed)
    assert grant["user_id"] == colleague["id"], (
        "a case-sensitive lookup would have missed a real account here"
    )
    assert grant["user_email"] == colleague["email"]
    assert grant["package_id"] == package_uuid
    assert grant["granted_by_user_id"] == publisher["id"]

    entry = entry_for(list_skill_catalog(client, col_headers), package_uuid)
    assert entry is not None, "the grant is what makes the package visible"
    assert entry["is_granted"] is True
    assert entry["can_manage"] is False
    assert get_skill_package(client, col_headers, package_uuid)["name"] == "team-notes"
    assert get_revision_content(client, col_headers, package_uuid, 1)["content"]

    own_entry = entry_for(list_skill_catalog(client, pub_headers), package_uuid)
    assert own_entry["is_granted"] is False, (
        "'Shared with you' must not appear on your own publication"
    )

    # ── Phase 3: nobody else, administrator included ─────────────────────
    for headers in (stranger_headers, superuser_token_headers):
        assert package_uuid not in catalog_ids(list_skill_catalog(client, headers))
        assert (
            error_code(
                get_skill_package(client, headers, package_uuid, expected_status=404)
            )
            == "package_not_found"
        )

    # ── Phase 4: the grant routes' refusals ──────────────────────────────
    again = add_skill_package_grant(
        client, pub_headers, package_uuid, colleague["email"]
    )
    assert again["id"] == grant["id"], "re-granting is idempotent, not a refusal"
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 1

    self_grant = add_skill_package_grant(
        client, pub_headers, package_uuid, publisher["email"], expected_status=409
    )
    assert error_code(self_grant) == "self_grant"

    unknown = add_skill_package_grant(
        client,
        pub_headers,
        package_uuid,
        f"nobody-{uuid.uuid4().hex}@example.com",
        expected_status=404,
    )
    assert error_code(unknown) == "user_not_found"

    absent = revoke_skill_package_grant(
        client, pub_headers, package_uuid, str(uuid.uuid4()), expected_status=404
    )
    assert error_code(absent) == "grant_not_found"

    # The colleague CAN see the package, and still may not manage who else does.
    assert (
        error_code(
            list_skill_package_grants(
                client, col_headers, package_uuid, expected_status=403
            )
        )
        == "not_publisher"
    )
    assert (
        error_code(
            add_skill_package_grant(
                client,
                col_headers,
                package_uuid,
                _stranger["email"],
                expected_status=403,
            )
        )
        == "not_publisher"
    )
    assert (
        error_code(
            revoke_skill_package_grant(
                client,
                col_headers,
                package_uuid,
                colleague["id"],
                expected_status=403,
            )
        )
        == "not_publisher"
    )
    # A stranger is not told the package exists at all.
    assert (
        error_code(
            list_skill_package_grants(
                client, stranger_headers, package_uuid, expected_status=404
            )
        )
        == "package_not_found"
    )

    # ── Phase 5: a grant is not a way around `is_listed` ─────────────────
    # The publisher's own lever first.
    update_skill_package(client, pub_headers, package_uuid, is_listed=False)
    assert package_uuid not in catalog_ids(list_skill_catalog(client, col_headers)), (
        "an unlisted package leaves a granted user's catalog, exactly like a "
        "public one — a grant is the publisher's lever, not a bypass"
    )
    assert (
        error_code(
            get_skill_package(client, col_headers, package_uuid, expected_status=404)
        )
        == "package_not_found"
    )
    assert package_uuid in catalog_ids(list_skill_catalog(client, pub_headers)), (
        "the publisher always sees their own"
    )
    update_skill_package(client, pub_headers, package_uuid, is_listed=True)
    assert package_uuid in catalog_ids(list_skill_catalog(client, col_headers))

    # ── Phase 5b: the administrator's lever reaches a `users` package ────
    # An admin is deliberately NOT on the allowlist — they cannot read this
    # package (asserted in Phase 3) and hold no grant on it. Delist is loaded
    # without a visibility check for exactly that reason: otherwise a publisher
    # could put a harmful skill in front of named colleagues and nobody could
    # pull it from circulation.
    delisted = delist_skill_package(client, superuser_token_headers, package_uuid)
    assert delisted["is_listed"] is False
    assert get_skill_package(client, pub_headers, package_uuid)["is_listed"] is False
    assert package_uuid not in catalog_ids(list_skill_catalog(client, col_headers)), (
        "delisting must reach the granted users, or the lever is decorative"
    )
    assert package_uuid in catalog_ids(list_skill_catalog(client, pub_headers))
    # Idempotent, and the publisher can put it back.
    delist_skill_package(client, superuser_token_headers, package_uuid)
    update_skill_package(client, pub_headers, package_uuid, is_listed=True)
    assert package_uuid in catalog_ids(list_skill_catalog(client, col_headers))

    # ── Phase 6: re-publish and visibility changes keep the grants ───────
    write_skill(env_id, "team-notes", description="Files the team note.", body="v2")
    publish_skill(client, pub_headers, agent_id, "team-notes", version="2.0")
    grants = list_skill_package_grants(client, pub_headers, package_uuid)
    assert grants["count"] == 1
    assert grant_emails_of(grants) == {colleague["email"]}

    update_skill_package(client, pub_headers, package_uuid, visibility="public")
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 1, (
        "moving to public keeps the rows — the change is reversible"
    )
    update_skill_package(client, pub_headers, package_uuid, visibility="users")
    assert entry_for(
        list_skill_catalog(client, col_headers), package_uuid
    ) is not None

    # ── Phase 7: revoking hides it ───────────────────────────────────────
    revoke_skill_package_grant(client, pub_headers, package_uuid, colleague["id"])
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 0
    assert package_uuid not in catalog_ids(list_skill_catalog(client, col_headers))
    assert (
        error_code(
            get_skill_package(client, col_headers, package_uuid, expected_status=404)
        )
        == "package_not_found"
    )


# ── Scenario 7: publishing with `grant_emails` ─────────────────────────────


def test_publish_grant_emails_are_additive_and_all_or_nothing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Sharing at publish time, in the same transaction as the revision:

      1. First publish names one colleague AND the publisher's own address →
         the colleague is granted, the publisher's entry is SKIPPED rather than
         refused (inside a publish it names a no-op; on the dedicated grant
         route the same address is a 409, which Scenario 6 pins).
      2. The colleague can see the package immediately — the grants and the
         revision landed together.
      3. A re-publish naming a second colleague is ADDITIVE: two grants, the
         first untouched. A re-publish naming nobody revokes nothing.
      4. One address that resolves to no account fails the WHOLE publish: no
         second package for a new skill name, and no extra revision on an
         existing one. A typo must not leave an immutable revision behind.
      5. The list is capped at fifty by the schema: fifty-one is a 422 that
         writes nothing, fifty is accepted.
    """
    publisher, pub_headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, pub_headers, "Grant-Emails")
    write_skill(env_id, "shared-report", description="Shares a report.")

    first, first_headers = create_random_user_with_headers(client)
    second, _ = create_random_user_with_headers(client)

    # ── Phase 1: the publisher's own address is skipped, spellings fold ──
    # One account named twice — padded and mixed-case, then plainly — plus the
    # publisher themselves. Addresses are trimmed, lowercased and deduplicated
    # BEFORE any lookup, so a dialog submitted twice costs one query and
    # produces one grant rather than one per spelling.
    publish_skill(
        client,
        pub_headers,
        agent_id,
        "shared-report",
        version="1.0",
        visibility="users",
        grant_emails=[
            f"  {mixed_case(first['email'])}  ",
            first["email"],
            publisher["email"],
        ],
    )
    package_uuid = list_skill_catalog(client, pub_headers)[0]["id"]
    grants = list_skill_package_grants(client, pub_headers, package_uuid)
    assert grants["count"] == 1, (
        "two spellings of one account are one grant, and the publisher already "
        "sees their own skill — naming themselves is a no-op, not a failure"
    )
    assert grant_emails_of(grants) == {first["email"]}, (
        "the grant is keyed on the account, so it reports the stored address "
        "rather than whatever the publisher typed"
    )

    # ── Phase 2: visible right away ──────────────────────────────────────
    entry = entry_for(list_skill_catalog(client, first_headers), package_uuid)
    assert entry is not None and entry["is_granted"] is True

    # ── Phase 3: additive across re-publishes ────────────────────────────
    write_skill(env_id, "shared-report", description="Shares a report.", body="v2")
    publish_skill(
        client,
        pub_headers,
        agent_id,
        "shared-report",
        version="2.0",
        grant_emails=[second["email"]],
    )
    grants = list_skill_package_grants(client, pub_headers, package_uuid)
    assert grants["count"] == 2
    assert grant_emails_of(grants) == {first["email"], second["email"]}

    write_skill(env_id, "shared-report", description="Shares a report.", body="v3")
    publish_skill(
        client, pub_headers, agent_id, "shared-report", version="3.0", grant_emails=[]
    )
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 2, (
        "a publish never revokes — an out-of-date dialog must not be able to "
        "take access away"
    )
    assert get_skill_package(client, pub_headers, package_uuid)[
        "latest_revision_number"
    ] == 3

    # ── Phase 4: one bad address fails the whole publish ─────────────────
    ghost_email = f"nobody-{uuid.uuid4().hex}@example.com"

    write_skill(env_id, "second-skill", description="Never gets published.")
    refused = publish_skill(
        client,
        pub_headers,
        agent_id,
        "second-skill",
        version="1.0",
        visibility="users",
        grant_emails=[first["email"], ghost_email],
        expected_status=404,
    )
    assert error_code(refused) == "user_not_found"
    assert not [
        e for e in list_skill_catalog(client, pub_headers) if e["name"] == "second-skill"
    ], "a typo must not leave a half-published package behind"

    write_skill(env_id, "shared-report", description="Shares a report.", body="v4")
    refused = publish_skill(
        client,
        pub_headers,
        agent_id,
        "shared-report",
        version="4.0",
        grant_emails=[ghost_email],
        expected_status=404,
    )
    assert error_code(refused) == "user_not_found"
    assert get_skill_package(client, pub_headers, package_uuid)[
        "latest_revision_number"
    ] == 3, "the refused publish must not have appended a revision"
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 2

    # ── Phase 5: the list is bounded at the schema ───────────────────────
    # Fifty is well past a hand-filled dialog; a wider audience is what
    # visibility="public" is for. The cap is a schema rule, so it fires as a
    # 422 before the route body runs — no lookups, nothing written.
    write_skill(env_id, "shared-report", description="Shares a report.", body="v5")
    too_many = publish_skill(
        client,
        pub_headers,
        agent_id,
        "shared-report",
        version="5.0",
        grant_emails=[f"user-{n}@example.com" for n in range(51)],
        expected_status=422,
    )
    assert "grant_emails" in str(too_many["detail"]), (
        "the validation error must name the field the caller has to shorten"
    )
    assert get_skill_package(client, pub_headers, package_uuid)[
        "latest_revision_number"
    ] == 3, "a refused body must not have appended a revision"
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 2

    # Fifty exactly is accepted — the boundary is a cap, not an off-by-one.
    publish_skill(
        client,
        pub_headers,
        agent_id,
        "shared-report",
        version="5.0",
        grant_emails=[first["email"]] * 50,
        expected_status=200,
    )
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 2, (
        "fifty repetitions of one address is still one account"
    )


# ── Scenario 8: grant emails require `users` visibility ────────────────────


def test_grant_emails_are_refused_unless_the_visibility_is_users(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A grant row only does something under ``visibility='users'``: that is the
    one branch ``user_can_see`` and ``is_granted`` consult. Written under any
    other visibility the rows are inert — but latent, and a later visibility
    change to ``users`` turns access nobody asked for on. So the publish
    refuses instead, before the first write:

      1. First publish of a never-published skill, addresses named, visibility
         omitted → the effective visibility is the new-package default
         ``private``: 409, and nothing is published at all.
      2. ``visibility='public'`` with addresses → 409, still nothing published.
         An address that resolves to nobody is reported ahead of this refusal:
         the addresses are resolved first, so a typo is named before the
         combination is judged.
      3. ``visibility='users'`` with the same addresses → published and granted.
         The refusal is about the combination, not about the field.
      4. A re-publish that names addresses and OMITS the visibility is allowed
         on the now-``users`` package: "leave the visibility alone" is a
         documented shape and must keep working.
      5. A re-publish that flips the same package to ``public`` while naming an
         address → 409, and no revision was appended.
      6. Naming only the publisher's own address is not a refusal under any
         visibility: it resolves to no grant row, so there is nothing latent to
         refuse.
      7. Omitting the visibility does NOT excuse the combination: a re-publish
         that names an address on an already-``public`` package inherits
         ``public`` and is refused. This is the shape a second client — the
         CLI, a script — would send, and the one the "leave the visibility
         alone" allowance in step 4 must not widen into a hole.
      8. The standalone grant route is untouched by all of this — granting on a
         ``private`` package ahead of flipping the visibility is how a
         publisher prepares one.
    """
    publisher, pub_headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, pub_headers, "Grant-Visibility")
    write_skill(env_id, "gated-report", description="Shares a report.")

    colleague, _ = create_random_user_with_headers(client)

    # ── Phase 1: omitted visibility on a first publish means private ──────
    refused = publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="1.0",
        grant_emails=[colleague["email"]],
        expected_status=409,
    )
    assert error_code(refused) == "grants_require_users_visibility"
    assert "users" in refused["detail"]["message"], (
        "the sentence has to name the visibility the caller should set"
    )
    assert list_skill_catalog(client, pub_headers) == [], (
        "the refusal fires before the package row is committed"
    )

    # ── Phase 2: an explicit non-users visibility is refused the same way ──
    refused = publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="1.0",
        visibility="public",
        grant_emails=[colleague["email"]],
        expected_status=409,
    )
    assert error_code(refused) == "grants_require_users_visibility"
    assert list_skill_catalog(client, pub_headers) == []

    # ...and so is an explicit ``private``, which completes the matrix: the
    # rule is about the effective visibility, not about which of the two ways
    # of arriving at a non-``users`` one the caller took.
    refused = publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="1.0",
        visibility="private",
        grant_emails=[colleague["email"]],
        expected_status=409,
    )
    assert error_code(refused) == "grants_require_users_visibility"
    assert list_skill_catalog(client, pub_headers) == []

    # A bad address still wins over this refusal: ``_resolve_grant_targets``
    # runs first, so a caller with a typo AND a contradictory visibility is told
    # about the typo. Pinned because the two refusals are adjacent and the order
    # is a choice — the addresses have to resolve before "would these rows do
    # anything" is even a well-posed question.
    refused = publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="1.0",
        visibility="public",
        grant_emails=[f"nobody-{uuid.uuid4().hex}@example.com"],
        expected_status=404,
    )
    assert error_code(refused) == "user_not_found"
    assert list_skill_catalog(client, pub_headers) == []

    # ── Phase 3: the same request with users visibility goes through ───────
    publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="1.0",
        visibility="users",
        grant_emails=[colleague["email"]],
    )
    package_uuid = list_skill_catalog(client, pub_headers)[0]["id"]
    assert grant_emails_of(
        list_skill_package_grants(client, pub_headers, package_uuid)
    ) == {colleague["email"]}

    # ── Phase 4: omitted visibility on an already-users package is fine ────
    second, _ = create_random_user_with_headers(client)
    write_skill(env_id, "gated-report", description="Shares a report.", body="v2")
    publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="2.0",
        grant_emails=[second["email"]],
    )
    grants = list_skill_package_grants(client, pub_headers, package_uuid)
    assert grant_emails_of(grants) == {colleague["email"], second["email"]}

    # ── Phase 5: flipping to public while naming an address is refused ─────
    third, _ = create_random_user_with_headers(client)
    write_skill(env_id, "gated-report", description="Shares a report.", body="v3")
    refused = publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="3.0",
        visibility="public",
        grant_emails=[third["email"]],
        expected_status=409,
    )
    assert error_code(refused) == "grants_require_users_visibility"
    assert get_skill_package(client, pub_headers, package_uuid)[
        "latest_revision_number"
    ] == 2, "the refused publish must not have appended a revision"
    assert get_skill_package(client, pub_headers, package_uuid)["visibility"] == (
        "users"
    ), "and must not have changed the visibility either"
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 2

    # ── Phase 6: the publisher's own address writes nothing, so it passes ──
    publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="3.0",
        visibility="public",
        grant_emails=[publisher["email"]],
    )
    assert get_skill_package(client, pub_headers, package_uuid)["visibility"] == (
        "public"
    )
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 2

    # ── Phase 7: omitting the visibility inherits public, and is refused ───
    # The package is ``public`` after Phase 6. The allowance in Phase 4 is
    # "leave the visibility alone", not "grants are fine when unstated" — the
    # effective value comes from the package that already exists, and here that
    # is ``public``. Without this the one shape a non-browser client is most
    # likely to send (grant_emails, no visibility field) walks straight past
    # the guard onto a public package.
    fourth, _ = create_random_user_with_headers(client)
    write_skill(env_id, "gated-report", description="Shares a report.", body="v4")
    refused = publish_skill(
        client,
        pub_headers,
        agent_id,
        "gated-report",
        version="4.0",
        grant_emails=[fourth["email"]],
        expected_status=409,
    )
    assert error_code(refused) == "grants_require_users_visibility"
    assert "public" in refused["detail"]["message"], (
        "the sentence has to name the visibility this publish would leave, "
        "which the caller never wrote down"
    )
    package_now = get_skill_package(client, pub_headers, package_uuid)
    assert package_now["latest_revision_number"] == 3, (
        "the refusal fires before the revision is appended"
    )
    assert package_now["visibility"] == "public"
    assert list_skill_package_grants(client, pub_headers, package_uuid)["count"] == 2

    # ── Phase 8: the standalone grant route stays permissive ───────────────
    # The package is public right now; granting on it is preparation for a
    # later flip back to users, and the route has no business refusing it.
    update_skill_package(client, pub_headers, package_uuid, visibility="private")
    add_skill_package_grant(client, pub_headers, package_uuid, third["email"])
    assert grant_emails_of(
        list_skill_package_grants(client, pub_headers, package_uuid)
    ) == {colleague["email"], second["email"], third["email"]}
