"""Helpers for the team library tests (API wrappers + a real git client).

Two kinds of helper live here:

* thin API wrappers (``create_library_agent``, ``put_grants`` ...) following
  the usual ``tests/utils`` convention;
* :class:`GitClient`, a real ``git`` subprocess wrapper pointed at the loopback
  bridge in ``tests/utils/git_http_bridge.py``. Credentials never touch a
  keychain: the token travels in an ``http.extraHeader`` injected through
  ``GIT_CONFIG_COUNT/KEY/VALUE``, prompts are off and ``credential.helper`` is
  emptied, and the global/system git config is ignored.

``bind_cloud_install_seam`` is a documented Rule-1 exemption: a cloud install
bound to a library repo is made by a git checkout flow that needs a remote
clone, so the test inserts the ``AgentGitSource`` row directly.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string

_BASE = f"{settings.API_V1_STR}/library"

_KIT_VERSION_FILE = (
    Path(__file__).resolve().parents[2]
    / "app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/CONTRACT_VERSION"
)


# ── People ────────────────────────────────────────────────────────────


@dataclass
class Person:
    id: str
    email: str
    headers: dict[str, str]

    @property
    def token(self) -> str:
        return self.headers["Authorization"].removeprefix("Bearer ")


def make_person(
    client: TestClient, superuser_headers: dict[str, str], role: str | None = None
) -> Person:
    """A signed-up user; ``role`` (``agent-developer`` / ``admin``) set by the superuser."""
    from tests.utils.user import create_random_user_with_headers

    user, headers = create_random_user_with_headers(client)
    if role is not None:
        r = client.patch(
            f"{settings.API_V1_STR}/users/{user['id']}/role",
            headers=superuser_headers,
            json={"role": role},
        )
        assert r.status_code == 200, f"Set role failed: {r.text}"
    return Person(id=user["id"], email=user["email"], headers=headers)


# ── API wrappers ──────────────────────────────────────────────────────


def random_slug() -> str:
    return f"lib-{random_lower_string()[:10]}"


def create_library_agent(
    client: TestClient,
    headers: dict[str, str],
    slug: str | None = None,
    name: str = "Library Agent",
    description: str = "Shared with the team",
) -> dict:
    r = client.post(
        f"{_BASE}/agents",
        headers=headers,
        json={"slug": slug or random_slug(), "name": name, "description": description},
    )
    assert r.status_code == 200, f"Create library agent failed: {r.text}"
    return r.json()


def get_grants(client: TestClient, headers: dict[str, str], repo_id: str) -> list[dict]:
    r = client.get(f"{_BASE}/agents/{repo_id}/grants", headers=headers)
    assert r.status_code == 200, f"Get grants failed: {r.text}"
    return r.json()


def put_grants(
    client: TestClient, headers: dict[str, str], repo_id: str, grants: list[dict]
) -> list[dict]:
    r = client.put(f"{_BASE}/agents/{repo_id}/grants", headers=headers, json=grants)
    assert r.status_code == 200, f"Put grants failed: {r.text}"
    return r.json()


def user_grant(user_id: str, access: str) -> dict:
    return {"principal": {"user_id": user_id}, "access": access}


def role_grant(role: str, access: str) -> dict:
    return {"principal": {"role": role}, "access": access}


def everyone_grant(access: str) -> dict:
    return {"principal": "everyone", "access": access}


def list_library(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get(_BASE, headers=headers)
    assert r.status_code == 200, f"List library failed: {r.text}"
    return r.json()


def library_slugs(listing: dict) -> set[str]:
    return {a["slug"] for a in listing["agents"]}


def delete_library_agent(client: TestClient, headers: dict[str, str], repo_id: str):
    return client.delete(f"{_BASE}/agents/{repo_id}", headers=headers)


def bind_cloud_install_seam(
    db, agent_id: str, owner_id: str, repo_id: str, host: str | None = None
) -> None:
    """DB seam: bind a cloud agent to a library repo via its ``AgentGitSource``."""
    from app.models.bundles.agent_git_source import AgentGitSource

    db.add(
        AgentGitSource(
            agent_id=uuid.UUID(agent_id),
            owner_id=uuid.UUID(owner_id),
            repo_url=f"{host or settings.backend_base_url}/git/{repo_id}.git",
        )
    )
    db.commit()


# ── Agent tree on disk ────────────────────────────────────────────────


def server_contract_version() -> tuple[int, int, int]:
    major, minor, patch = _KIT_VERSION_FILE.read_text().strip().split("-")[0].split(".")
    return int(major), int(minor), int(patch)


def write_agent_tree(
    path: Path,
    slug: str = "my-agent",
    *,
    contract_version: str | None = None,
    manifest: dict | None | bool = True,
    files: dict[str, bytes | str] | None = None,
) -> Path:
    """Write a kit folder (tree v3: ``cinna-agent.json`` at the root).

    ``manifest=False`` omits the manifest; a dict replaces it entirely.
    """
    path.mkdir(parents=True, exist_ok=True)
    if manifest is not False:
        if isinstance(manifest, dict):
            document = manifest
        else:
            major, minor, patch = server_contract_version()
            document = {
                "id": str(uuid.uuid4()),
                "name": "My Agent",
                "slug": slug,
                "description": "A test agent",
                "contract_version": contract_version or f"{major}.{minor}.{patch}",
            }
        (path / "cinna-agent.json").write_text(json.dumps(document, indent=2))
    for rel, content in {"AGENTS.md": "# My agent\n", **(files or {})}.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


# ── Real git client ───────────────────────────────────────────────────


@dataclass
class GitResult:
    returncode: int
    output: str  # stdout + stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitClient:
    """A ``git`` subprocess configured for one identity against the bridge."""

    def __init__(
        self,
        root: Path,
        *,
        token: str | None = None,
        auth: str = "bearer",
        protocol: str | None = None,
        extra_headers: list[str] | None = None,
    ) -> None:
        self.root = root
        self.home = root / f"home-{uuid.uuid4().hex[:6]}"
        self.home.mkdir(parents=True, exist_ok=True)
        pairs: list[tuple[str, str]] = [
            ("credential.helper", ""),
            ("init.defaultBranch", "main"),
            ("advice.detachedHead", "false"),
        ]
        if protocol:
            pairs.append(("protocol.version", protocol))
        if token:
            if auth == "basic":
                raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
                header = f"Authorization: Basic {raw}"
            else:
                header = f"Authorization: Bearer {token}"
            pairs.append(("http.extraHeader", header))
        for extra in extra_headers or []:
            pairs.append(("http.extraHeader", extra))
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.com",
            "GIT_CONFIG_COUNT": str(len(pairs)),
        }
        for i, (key, value) in enumerate(pairs):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        self.env = env

    def run(self, *args: str, cwd: Path | None = None) -> GitResult:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return GitResult(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))

    def must(self, *args: str, cwd: Path | None = None) -> GitResult:
        result = self.run(*args, cwd=cwd)
        assert result.ok, f"git {' '.join(args)} failed:\n{result.output}"
        return result

    def clone(self, url: str, dest: Path) -> GitResult:
        return self.run("clone", url, str(dest))

    def commit_all(self, repo: Path, message: str = "change") -> str:
        if not (repo / ".git").exists():
            self.must("init", "-b", "main", cwd=repo)
        self.must("add", "-A", cwd=repo)
        self.must("commit", "-q", "--allow-empty", "-m", message, cwd=repo)
        return self.must("rev-parse", "HEAD", cwd=repo).output.strip()

    def push(self, repo: Path, *refspec: str, url: str | None = None) -> GitResult:
        target = [url] if url else ["origin"]
        return self.run("push", *target, *(refspec or ("HEAD:refs/heads/main",)), cwd=repo)

    def add_remote(self, repo: Path, url: str) -> None:
        self.must("remote", "add", "origin", url, cwd=repo)
