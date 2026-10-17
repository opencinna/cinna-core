"""
Publishing artifacts out of an agent environment (P6, C2).

``POST /agent/artifacts/publish`` is what the environment's ``artifacts_publish``
tool calls (env token + ``X-Agent-Env-Id``); ``POST /artifacts/from-environment``
is the owner's "Publish as artifact" in the workspace file view (user JWT).
Workspace items are served by ``EnvironmentTestAdapter`` (``workspace_files``;
a folder is a zip with ``Content-Type: application/zip``, as the env server
sends it).

Scenarios:
  1. Agent publish: folder ⇒ private site, ``excluded`` listed; same path again
     ⇒ same artifact, revision 2 (A12); ``artifact_id`` republish of another
     path; foreign / upload / other owner's ``artifact_id`` ⇒ 404; extra
     ``visibility`` ⇒ 422 (A8); missing path ⇒ 404; traversal ⇒ 400; a path
     that is itself A10-excluded or dot-named ⇒ 400; what the env's artifact
     zipper reports as skipped is merged into ``excluded``; other users cannot
     see the result.
  2. Environment state and the owner's workspace publish: stopped env ⇒ 409
     on both routes; from-environment republishes what the agent published at
     the same path and carries ``excluded``; a second environment of the same
     agent republishes the same path (A12 keys by agent); a non-owner and an
     unknown env ⇒ 404; excluded paths ⇒ 400; a session of another agent ⇒ 403.
  2b. The kit ``artifacts/`` folder role (contract 1.13.0): ``artifacts/<name>``
     (a folder or a single file) publishes; every spelling of the same path
     republishes the same artifact, from the agent and from the owner's
     workspace view; ``app-data/`` is still refused (A10 unchanged).
  3. Display fields the web app reads: ``source_session_id`` (owner only),
     revisions' ``created_by_name``, grants' ``email`` (owner only), link
     info's ``description``.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_env import create_env_with_token
from tests.utils.artifacts import (
    agent_publish,
    create_link,
    link_info,
    set_grants,
    build_dotenv_zip,
    build_site_zip,
    clear_workspace_items,
    get_artifact,
    publish,
    publish_from_environment,
    stage_workspace_item,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.fixtures import (
    patch_asyncio_to_thread,
    patched_background_tasks,
    patched_create_sessions,
    patched_external_services,
    setup_default_credentials,
    setup_environment_adapter,
    teardown_environment_adapter,
    BACKGROUND_TASK_TARGETS_FULL,
    CREATE_SESSION_TARGETS_AGENT,
)
from tests.utils.session import create_session_via_api
from tests.utils.user import create_random_user_with_headers

# Re-export importable autouse fixtures (agent creation needs them).
patch_asyncio_to_thread = patch_asyncio_to_thread
setup_default_credentials = setup_default_credentials

_API = settings.API_V1_STR
_SITE = "reports/dash"
_NOTES = "notes/summary.md"
_SECRETS_SITE = "reports/with-secrets"
_LINKY_SITE = "reports/with-links"
# The kit's ``artifacts`` folder role: one subfolder (or file) per artifact.
_ARTIFACTS_SITE = "artifacts/q3-dashboard"
_ARTIFACTS_FILE = "artifacts/summary.md"
_APP_DATA_SITE = "app-data/reports/q3.html"
# A path the env refuses in artifact mode (it is, or runs through, a symlink).
_LINKED_PATH = "reports/link.json"
_LINK_REFUSAL = "Invalid path: Symlinks are not published: reports/link.json"
# Paths the A10 rules exclude (or dot-named): refused before any pull.
_EXCLUDED_PATHS = (
    "credentials",
    "credentials/credentials.json",
    ".claude/settings.json",
    ".bashrc",
    "app-data/storage/STATUS.md",
    "web/node_modules/lib",
    "keys/server.pem",
)


@pytest.fixture(autouse=True)
def patch_create_session(db):
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    lm = setup_environment_adapter(tmp_path_factory)
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def background_tasks():
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL):
        yield


@pytest.fixture(autouse=True)
def patch_external_services():
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield


@pytest.fixture(autouse=True)
def workspace_items():
    stage_workspace_item(_SITE, build_site_zip(), folder=True)
    stage_workspace_item(_SECRETS_SITE, build_dotenv_zip(), folder=True)
    stage_workspace_item(_NOTES, b"# Summary\n\nAll good.\n")
    stage_workspace_item(
        _LINKY_SITE, build_site_zip(), folder=True, skipped=[".cache/", "secret-link", ".npmrc"],
    )
    for path in _EXCLUDED_PATHS:
        stage_workspace_item(path, b"SECRET=1")
    stage_workspace_item(_LINKED_PATH, b"{}", artifact_error=_LINK_REFUSAL)
    stage_workspace_item(_ARTIFACTS_SITE, build_site_zip(), folder=True)
    stage_workspace_item(_ARTIFACTS_FILE, b"# Q3 summary\n")
    stage_workspace_item(_APP_DATA_SITE, b"<html>scratch</html>")
    yield
    clear_workspace_items(
        _SITE, _SECRETS_SITE, _NOTES, _LINKY_SITE, _LINKED_PATH,
        _ARTIFACTS_SITE, _ARTIFACTS_FILE, _APP_DATA_SITE, *_EXCLUDED_PATHS,
    )


def _owner_id(client: TestClient, headers: dict[str, str]) -> str:
    r = client.get(f"{_API}/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()["id"]


def test_agent_publishes_privately_and_republishes_by_path(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="Dashboard Builder")
    drain_tasks()
    env, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # ── Phase 1: a folder becomes a private site ─────────────────────────
    first = agent_publish(client, env_headers, path=_SITE, title="Sales dashboard")
    artifact_id = first["artifact_id"]
    assert set(first) == {"artifact_id", "revision", "url", "kind", "file_count", "size_bytes", "excluded"}
    assert first["revision"] == 1
    assert first["kind"] == "site"
    assert first["file_count"] == 3
    assert first["size_bytes"] > 0
    assert first["excluded"] == []
    assert first["url"].endswith(f"/artifact/{artifact_id}")

    detail = get_artifact(client, headers, artifact_id)
    assert detail["visibility"] == "private"
    assert detail["source_kind"] == "core_agent"
    assert detail["source_agent_id"] == agent["id"]
    assert detail["source_label"] == "Dashboard Builder"
    assert detail["entry_path"] == "index.html"

    # ── Phase 2: the same path again ⇒ same artifact, revision 2 (A12) ───
    # The path forms an agent may use all resolve to the same workspace item.
    second = agent_publish(client, env_headers, path=f"/app/workspace/{_SITE}/", title="Sales dashboard v2")
    assert second["artifact_id"] == artifact_id
    assert second["revision"] == 2
    detail = get_artifact(client, headers, artifact_id)
    assert detail["title"] == "Sales dashboard v2"
    assert detail["revision_count"] == 2

    # ── Phase 3: republish into the artifact from another path ───────────
    third = agent_publish(
        client, env_headers, path=f"./{_NOTES}", title="Summary", artifact_id=artifact_id,
    )
    assert third["artifact_id"] == artifact_id
    assert third["revision"] == 3
    assert third["kind"] == "files"
    assert third["file_count"] == 1

    # ── Phase 4: the ingest's A10 rules still apply; excluded is returned ─
    secrets = agent_publish(client, env_headers, path=_SECRETS_SITE, title="With secrets")
    assert secrets["artifact_id"] != artifact_id
    assert secrets["excluded"] == [".env"]

    # ── Phase 5: A8 — no visibility, grants or links on this route ───────
    r = client.post(f"{_API}/agent/artifacts/publish", headers=env_headers, json={
        "path": _SITE, "title": "x", "visibility": "instance",
    })
    assert r.status_code == 422, r.text
    r = client.post(f"{_API}/agent/artifacts/publish", headers=env_headers, json={
        "path": _SITE, "title": "x", "grants": [],
    })
    assert r.status_code == 422, r.text
    assert get_artifact(client, headers, artifact_id)["visibility"] == "private"

    # ── Phase 6: artifact_id the agent did not publish ⇒ 404 ─────────────
    uploaded = publish(client, headers, files={"a.md": b"# hi"}, title="Uploaded by hand")
    agent_publish(client, env_headers, path=_SITE, title="x", artifact_id=uploaded["id"], expect=404)
    agent_publish(client, env_headers, path=_SITE, title="x", artifact_id=str(uuid.uuid4()), expect=404)

    other_agent = create_agent_via_api(client, headers, name="Other Builder")
    drain_tasks()
    _other_env, other_env_headers = create_env_with_token(db, agent_id=other_agent["id"], owner_id=owner_id)
    agent_publish(client, other_env_headers, path=_SITE, title="x", artifact_id=artifact_id, expect=404)

    _stranger, stranger_headers = create_random_user_with_headers(client)
    foreign = publish(client, stranger_headers, files={"b.md": b"# b"}, title="Stranger's")
    agent_publish(client, env_headers, path=_SITE, title="x", artifact_id=foreign["id"], expect=404)

    # ── Phase 7: path errors ─────────────────────────────────────────────
    agent_publish(client, env_headers, path="reports/missing", title="x", expect=404)
    agent_publish(client, env_headers, path="../etc/passwd", title="x", expect=400)
    agent_publish(client, env_headers, path="/app/workspace/", title="x", expect=400)
    # A10 applies to the published path itself: the env zips relative to the
    # folder, so the ingest alone would never see "credentials/".
    for path in _EXCLUDED_PATHS:
        agent_publish(client, env_headers, path=path, title="x", expect=400)

    # The env refuses a symlinked path; its message reaches the agent.
    refused = agent_publish(client, env_headers, path=_LINKED_PATH, title="x", expect=400)
    assert "Symlinks are not published" in refused["detail"]

    # ── Phase 7b: what the env's zipper left out is listed in excluded ───
    linky = agent_publish(client, env_headers, path=_LINKY_SITE, title="With links")
    assert linky["excluded"][:3] == [".cache/", "secret-link", ".npmrc"]
    assert get_artifact(client, headers, linky["artifact_id"])["revision_count"] == 1

    # ── Phase 8: the result is private to the owner ──────────────────────
    get_artifact(client, stranger_headers, artifact_id, expect=404)
    r = client.post(f"{_API}/agent/artifacts/publish", json={"path": _SITE, "title": "x"})
    assert r.status_code in (401, 403)

    # Revision count proves no stray revision was added by a refused call.
    assert get_artifact(client, headers, artifact_id)["revision_count"] == 3


def test_environment_state_and_owner_workspace_publish(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="Report Writer")
    drain_tasks()
    env, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    stopped, stopped_headers = create_env_with_token(
        db, agent_id=agent["id"], owner_id=owner_id, status="stopped",
    )

    # ── Phase 1: a stopped environment cannot be read ⇒ 409 ──────────────
    agent_publish(client, stopped_headers, path=_SITE, title="x", expect=409)
    publish_from_environment(
        client, headers, environment_id=str(stopped.id), path=_SITE, title="x", expect=409,
    )

    # ── Phase 2: the agent publishes, the owner republishes the same path ─
    by_agent = agent_publish(client, env_headers, path=_SITE, title="Dash")
    by_owner = publish_from_environment(
        client, headers, environment_id=str(env.id), path=_SITE, title="Dash (owner)",
    )
    assert by_owner["id"] == by_agent["artifact_id"]
    assert by_owner["revision_number"] == 2
    assert by_owner["source_kind"] == "core_agent"
    assert by_owner["visibility"] == "private"
    assert by_owner["excluded"] == []

    owner_new = publish_from_environment(
        client, headers, environment_id=str(env.id), path=_SECRETS_SITE, title="Secrets",
    )
    assert owner_new["revision_number"] == 1
    assert owner_new["excluded"] == [".env"]
    r = client.post(f"{_API}/artifacts/from-environment", headers=headers, json={
        "environment_id": str(env.id), "path": _SITE, "title": "x", "visibility": "instance",
    })
    assert r.status_code == 422, r.text

    # ── Phase 3: only the agent's owner may publish from its workspace ───
    _other, other_headers = create_random_user_with_headers(client)
    publish_from_environment(
        client, other_headers, environment_id=str(env.id), path=_SITE, title="x", expect=404,
    )
    publish_from_environment(
        client, headers, environment_id=str(uuid.uuid4()), path=_SITE, title="x", expect=404,
    )

    for path in _EXCLUDED_PATHS[:3]:
        publish_from_environment(
            client, headers, environment_id=str(env.id), path=path, title="x", expect=400,
        )
    refused = publish_from_environment(
        client, headers, environment_id=str(env.id), path=_LINKED_PATH, title="x", expect=400,
    )
    assert "Symlinks are not published" in refused["detail"]

    # ── Phase 4: a session of another agent is refused ───────────────────
    agent_publish(
        client, env_headers, path=_SITE, title="x", source_session_id=str(uuid.uuid4()), expect=403,
    )
    assert get_artifact(client, headers, by_agent["artifact_id"])["revision_count"] == 2

    # ── Phase 5: A12 keys by agent — a recreated env republishes ─────────
    _new_env, new_env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    again = agent_publish(client, new_env_headers, path=_SITE, title="Dash after rebuild")
    assert again["artifact_id"] == by_agent["artifact_id"]
    assert again["revision"] == 3


def test_agent_publishes_from_the_artifacts_folder_and_republishes_by_path(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    """The ``artifacts/`` folder role: build there, publish, republish in place.

    1. ``artifacts/q3-dashboard`` (a folder) ⇒ private site, revision 1.
    2. ``./artifacts/q3-dashboard/`` and ``/app/workspace/artifacts/q3-dashboard``
       ⇒ the same artifact, revisions 2 and 3 (A12, one ``source_key``).
    3. ``artifacts/summary.md`` (a single file directly inside) ⇒ its own
       artifact; republished ⇒ same id, revision 2.
    4. The owner's "Publish as artifact" on the same path ⇒ the same artifact.
    5. ``app-data/`` is still never published (A10), and no stray revision.
    """
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="Quarterly Reporter")
    drain_tasks()
    env, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # ── Phase 1: a folder under artifacts/ becomes a private site ────────
    site = agent_publish(client, env_headers, path=_ARTIFACTS_SITE, title="Q3 dashboard")
    site_id = site["artifact_id"]
    assert site["revision"] == 1
    assert site["kind"] == "site"
    assert site["file_count"] == 3
    assert site["excluded"] == []
    detail = get_artifact(client, headers, site_id)
    assert detail["visibility"] == "private"
    assert detail["source_kind"] == "core_agent"
    assert detail["source_agent_id"] == agent["id"]
    assert detail["entry_path"] == "index.html"

    # ── Phase 2: alternate spellings of the same path ⇒ same artifact ────
    again = agent_publish(client, env_headers, path=f"./{_ARTIFACTS_SITE}/", title="Q3 dashboard v2")
    assert again["artifact_id"] == site_id
    assert again["revision"] == 2
    absolute = agent_publish(
        client, env_headers, path=f"/app/workspace/{_ARTIFACTS_SITE}", title="Q3 dashboard v3",
    )
    assert absolute["artifact_id"] == site_id
    assert absolute["revision"] == 3
    detail = get_artifact(client, headers, site_id)
    assert detail["title"] == "Q3 dashboard v3"
    assert detail["revision_count"] == 3

    # ── Phase 3: a single file directly inside artifacts/ ────────────────
    summary = agent_publish(client, env_headers, path=_ARTIFACTS_FILE, title="Q3 summary")
    summary_id = summary["artifact_id"]
    assert summary_id != site_id
    assert summary["revision"] == 1
    assert summary["kind"] == "files"
    assert summary["file_count"] == 1
    resummary = agent_publish(client, env_headers, path=f"./{_ARTIFACTS_FILE}", title="Q3 summary v2")
    assert resummary["artifact_id"] == summary_id
    assert resummary["revision"] == 2

    # ── Phase 4: the owner's workspace publish of the same path ──────────
    by_owner = publish_from_environment(
        client, headers, environment_id=str(env.id), path=_ARTIFACTS_SITE, title="Q3 (owner)",
    )
    assert by_owner["id"] == site_id
    assert by_owner["revision_number"] == 4
    assert by_owner["visibility"] == "private"

    # ── Phase 5: app-data/ stays never-published (A10 unchanged) ─────────
    for path in (_APP_DATA_SITE, f"./{_APP_DATA_SITE}", f"/app/workspace/{_APP_DATA_SITE}", "app-data"):
        agent_publish(client, env_headers, path=path, title="x", expect=400)
    publish_from_environment(
        client, headers, environment_id=str(env.id), path=_APP_DATA_SITE, title="x", expect=400,
    )
    # Refused before any pull, and nothing landed on the existing artifacts.
    agent_publish(client, env_headers, path=_APP_DATA_SITE, title="x", artifact_id=site_id, expect=400)
    assert get_artifact(client, headers, site_id)["revision_count"] == 4
    assert get_artifact(client, headers, summary_id)["revision_count"] == 2


def test_display_fields_for_the_web_app(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session,
) -> None:
    headers = superuser_token_headers
    owner_id = _owner_id(client, headers)
    agent = create_agent_via_api(client, headers, name="Chart Maker")
    drain_tasks()
    _env, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)
    session = create_session_via_api(client, headers, agent["id"])

    # ── Phase 1: the publishing conversation is on the owner's view ──────
    published = agent_publish(
        client, env_headers, path=_SITE, title="Charts", description="Weekly charts",
        source_session_id=session["id"],
    )
    artifact_id = published["artifact_id"]
    detail = get_artifact(client, headers, artifact_id)
    assert detail["source_session_id"] == session["id"]
    assert detail["revisions"][0]["created_by"] == owner_id
    assert detail["revisions"][0]["created_by_name"] == detail["owner_name"]

    # ── Phase 2: grants carry the grantee's email, to the owner only ─────
    grantee, grantee_headers = create_random_user_with_headers(client)
    grants = set_grants(client, headers, artifact_id, [grantee["id"]])
    assert grants[0]["email"] == grantee["email"]
    assert get_artifact(client, headers, artifact_id)["grants"][0]["email"] == grantee["email"]

    seen_by_grantee = get_artifact(client, grantee_headers, artifact_id)
    assert seen_by_grantee["source_session_id"] is None
    assert seen_by_grantee["grants"] is None
    assert seen_by_grantee["revisions"][0]["created_by_name"] == detail["owner_name"]

    # ── Phase 3: link info shows the description ─────────────────────────
    link = create_link(client, headers, artifact_id)
    info = link_info(client, link["token"])
    assert info["title"] == "Charts"
    assert info["description"] == "Weekly charts"
