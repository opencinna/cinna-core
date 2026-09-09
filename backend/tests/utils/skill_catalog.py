"""Helpers for the skills catalog (Phase 3) API tests.

Two kinds of helper live here:

* **HTTP wrappers** over the ``/skills/...`` and ``/agents/{id}/skills/...``
  routes, following the house convention — assert the status, return the parsed
  body, and take an ``expected_status`` so a refusal path can be asserted with
  the same call.
* **Filesystem seeding** of a publisher's ``skills/<name>/`` folder. Publishing
  reads the *host-side* workspace at
  ``ENV_INSTANCES_DIR/<env id>/app/workspace`` (never the container), which the
  agent-domain fixtures already point at a tmp tree — the same seam
  ``tests/api/agents/bundles/agents_bundles_skills_test.py`` uses.

Snapshot storage (``settings.SKILL_STORAGE_DIR``) is NOT redirected here: it is
per-test state, so each test module owns a ``skill_storage`` fixture built on
:func:`patched_skill_storage`.
"""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import list_environments
from tests.utils.user import (
    create_random_user_with_headers,
    promote_to_developer,
)

API = settings.API_V1_STR


# ── Storage seam ───────────────────────────────────────────────────────────


@contextmanager
def patched_skill_storage(root: Path):
    """Point ``SKILL_STORAGE_DIR`` at ``root`` for the duration of a test.

    Without it, publishing writes revision snapshots and archive caches under
    the container's real ``/app/data/skills`` (a host bind mount), leaving a
    directory per published package behind on every run.
    """
    root.mkdir(parents=True, exist_ok=True)
    with patch.object(settings, "SKILL_STORAGE_DIR", str(root)):
        yield root


# ── Workspace seeding ──────────────────────────────────────────────────────


def workspace_root(env_id: str) -> Path:
    """The host-side workspace of an environment instance."""
    return Path(settings.ENV_INSTANCES_DIR) / str(env_id) / "app" / "workspace"


def write_skill(
    env_id: str,
    name: str,
    *,
    description: str | None = None,
    frontmatter: str | None = None,
    body: str = "Step one.",
    extra: dict[str, str] | None = None,
) -> Path:
    """Write ``skills/<name>/SKILL.md`` (plus optional extra files) on disk.

    ``frontmatter`` replaces the generated block wholesale — that is how the
    invalid-skill refusals are set up (a name that disagrees with the folder,
    a missing description, ...).
    """
    skill_dir = workspace_root(env_id) / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    front = frontmatter if frontmatter is not None else (
        f"name: {name}\ndescription: {description or f'Does {name} things.'}"
    )
    (skill_dir / "SKILL.md").write_text(
        f"---\n{front}\n---\n\n# {name}\n\n{body}\n", encoding="utf-8"
    )
    for rel, content in (extra or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


# ── Actors ─────────────────────────────────────────────────────────────────


def make_developer(
    client: TestClient, superuser_headers: dict[str, str]
) -> tuple[dict, dict[str, str]]:
    """A fresh user who may create agents and publish, with a default AI cred.

    Publishers are deliberately random users rather than the superuser: the
    publish path takes a per-``(publisher, skill name)`` ``asyncio.Lock`` kept
    in a module-level dict, and a stable publisher id would hand the same lock
    object to two tests running on two different event loops.
    """
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def make_agent_with_env(
    client: TestClient, headers: dict[str, str], name: str
) -> tuple[str, str]:
    """Create an agent and return ``(agent_id, env_id)`` once its env exists."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    envs = list_environments(client, headers, agent["id"])
    assert envs["data"], f"agent {name} must have an environment"
    return agent["id"], envs["data"][0]["id"]


# ── Publish ────────────────────────────────────────────────────────────────


def publish_skill(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    name: str,
    *,
    version: str | None = None,
    release_notes: str | None = None,
    visibility: str | None = None,
    package_id: str | None = None,
    grant_emails: list[str] | None = None,
    expected_status: int = 200,
) -> dict:
    """POST ``/agents/{agent_id}/skills/{name}/publish``."""
    body: dict = {}
    if version is not None:
        body["version"] = version
    if release_notes is not None:
        body["release_notes"] = release_notes
    if visibility is not None:
        body["visibility"] = visibility
    if package_id is not None:
        body["package_id"] = package_id
    if grant_emails is not None:
        body["grant_emails"] = grant_emails
    r = client.post(
        f"{API}/agents/{agent_id}/skills/{name}/publish", headers=headers, json=body
    )
    assert r.status_code == expected_status, (
        f"publish {name}: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


# ── Catalog reads ──────────────────────────────────────────────────────────


def list_skill_catalog(
    client: TestClient, headers: dict[str, str]
) -> list[dict]:
    r = client.get(f"{API}/skills/catalog", headers=headers)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["count"] == len(payload["data"])
    return payload["data"]


def catalog_ids(entries: list[dict]) -> set[str]:
    return {e["id"] for e in entries}


def entry_for(entries: list[dict], package_uuid: str) -> dict | None:
    return next((e for e in entries if e["id"] == package_uuid), None)


def get_skill_package(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.get(f"{API}/skills/packages/{package_uuid}", headers=headers)
    assert r.status_code == expected_status, (
        f"get package: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def get_revision_content(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    revision_number: int,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.get(
        f"{API}/skills/packages/{package_uuid}/revisions/{revision_number}/content",
        headers=headers,
    )
    assert r.status_code == expected_status, (
        f"revision content: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def get_revision_files(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    revision_number: int,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.get(
        f"{API}/skills/packages/{package_uuid}/revisions/{revision_number}/files",
        headers=headers,
    )
    assert r.status_code == expected_status, (
        f"revision files: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def download_revision(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    revision_number: int,
    *,
    expected_status: int = 200,
):
    """The user-facing archive download. Returns the whole response.

    Not ``.json()`` like its neighbours: the body is a tarball, and the tests
    that call this assert on the bytes and on ``Content-Disposition``.
    """
    r = client.get(
        f"{API}/skills/packages/{package_uuid}/revisions/{revision_number}/download",
        headers=headers,
    )
    assert r.status_code == expected_status, (
        f"revision download: expected {expected_status}, got {r.status_code}"
    )
    return r


# ── Manage ─────────────────────────────────────────────────────────────────


def update_skill_package(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    *,
    expected_status: int = 200,
    **fields,
) -> dict:
    r = client.patch(
        f"{API}/skills/packages/{package_uuid}", headers=headers, json=fields
    )
    assert r.status_code == expected_status, (
        f"update package: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def delist_skill_package(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.post(
        f"{API}/skills/packages/{package_uuid}/delist", headers=headers
    )
    assert r.status_code == expected_status, (
        f"delist: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


# ── Install ────────────────────────────────────────────────────────────────


def install_skill(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    package_uuid: str,
    *,
    revision_number: int | None = None,
    conversation_mode: bool = True,
    building_mode: bool = True,
    expected_status: int = 200,
) -> dict:
    """POST ``/agents/{agent_id}/skills/install`` → ``PluginSyncResponse``."""
    body: dict = {
        "package_id": package_uuid,
        "conversation_mode": conversation_mode,
        "building_mode": building_mode,
    }
    if revision_number is not None:
        body["revision_number"] = revision_number
    r = client.post(
        f"{API}/agents/{agent_id}/skills/install", headers=headers, json=body
    )
    assert r.status_code == expected_status, (
        f"install: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def list_agent_plugins(
    client: TestClient, headers: dict[str, str], agent_id: str
) -> list[dict]:
    r = client.get(
        f"{API}/llm-plugins/agents/{agent_id}/plugins", headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def upgrade_agent_plugin(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    link_id: str,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.post(
        f"{API}/llm-plugins/agents/{agent_id}/plugins/{link_id}/upgrade",
        headers=headers,
    )
    assert r.status_code == expected_status, (
        f"upgrade: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def uninstall_agent_plugin(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    link_id: str,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.delete(
        f"{API}/llm-plugins/agents/{agent_id}/plugins/{link_id}", headers=headers
    )
    assert r.status_code == expected_status, (
        f"uninstall: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


# ── Access grants (``visibility='users'``) ─────────────────────────────────


def mixed_case(email: str) -> str:
    """The same address a colleague would type it — ``Jo.Blogs@Example.COM``.

    Accounts are stored lowercased, and every lookup that forgets to lowercase
    both sides silently fails to find a real user. Grant tests type the address
    genuinely mixed-case so that bug cannot pass here.
    """
    return "".join(
        ch.upper() if index % 2 == 0 else ch for index, ch in enumerate(email)
    )


def list_skill_package_grants(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    *,
    expected_status: int = 200,
) -> dict:
    """GET ``/skills/packages/{id}/grants``."""
    r = client.get(f"{API}/skills/packages/{package_uuid}/grants", headers=headers)
    assert r.status_code == expected_status, (
        f"list grants: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def add_skill_package_grant(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    email: str,
    *,
    expected_status: int = 200,
) -> dict:
    """POST ``/skills/packages/{id}/grants``."""
    r = client.post(
        f"{API}/skills/packages/{package_uuid}/grants",
        headers=headers,
        json={"email": email},
    )
    assert r.status_code == expected_status, (
        f"add grant: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return r.json()


def revoke_skill_package_grant(
    client: TestClient,
    headers: dict[str, str],
    package_uuid: str,
    user_id: str,
    *,
    expected_status: int = 204,
) -> dict | None:
    """DELETE ``/skills/packages/{id}/grants/{user_id}``."""
    r = client.delete(
        f"{API}/skills/packages/{package_uuid}/grants/{user_id}", headers=headers
    )
    assert r.status_code == expected_status, (
        f"revoke grant: expected {expected_status}, got {r.status_code}: {r.text}"
    )
    return None if r.status_code == 204 else r.json()


def grant_emails_of(payload: dict) -> set[str]:
    """The set of addresses a grants listing names."""
    return {g["user_email"] for g in payload["data"]}


# ── Error-body reads ───────────────────────────────────────────────────────


def error_code(response_json: dict) -> str:
    """The coded ``detail.code`` a ``SkillCatalogError`` travels in."""
    detail = response_json.get("detail")
    assert isinstance(detail, dict), f"expected a coded detail, got {detail!r}"
    return detail["code"]
