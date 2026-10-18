"""Helpers for skills-format marketplace snapshot tests.

Fixture-repo writers, a second-superuser factory (a superuser who does *not*
own a marketplace is the D3 "no superuser bypass" case), and read-only views of
the snapshot folder. The folder is the on-disk half of the contract
(``<SKILL_STORAGE_DIR>/marketplace-skills/<marketplace_id>/<plugin_id>/<sha>.tar.gz``)
and has no endpoint, so tests look at it directly with the stdlib.
"""
import hashlib
import io
import tarfile
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.llm_plugin import (
    PLUGINS_BASE,
    create_marketplace,
    discover_plugins,
    plugins_by_name,
    sync_marketplace,
)
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR


def write_skill(folder: Path, name: str, *, body: str = "Step one.") -> None:
    """A minimal valid ``SKILL.md`` in ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Does {name} things.\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def skills_repo(
    names: tuple[str, ...] = ("alpha", "beta", "gamma"),
    *,
    bodies: dict[str, str] | None = None,
    broken: tuple[str, ...] = ("half-done",),
    extra: Callable[[Path], None] | None = None,
) -> Callable[[Path], None]:
    """``populate`` for a ``skills/<name>/`` collection (+ folders with no SKILL.md)."""
    bodies = bodies or {}

    def populate(root: Path) -> None:
        for name in names:
            folder = root / "skills" / name
            write_skill(folder, name, body=bodies.get(name, "Step one."))
            (folder / "scripts").mkdir(exist_ok=True)
            run = folder / "scripts" / "run.sh"
            run.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            run.chmod(0o755)
        for name in broken:
            (root / "skills" / name).mkdir(parents=True, exist_ok=True)
            (root / "skills" / name / "README.md").write_text("todo", encoding="utf-8")
        if extra is not None:
            extra(root)

    return populate


def new_skills_marketplace(
    client: TestClient,
    headers: dict[str, str],
    populate: Callable[[Path], None],
    *,
    public_discovery: bool = True,
    commit_hash: str = "c0ffee000001",
) -> dict:
    """Register + sync a skills marketplace. Returns the marketplace row."""
    marketplace = create_marketplace(
        client,
        headers,
        url=f"https://github.com/example/skills-{uuid.uuid4().hex[:8]}.git",
        marketplace_type="skills",
        public_discovery=public_discovery,
    )
    synced = sync_marketplace(
        client, headers, marketplace["id"], populate=populate, commit_hash=commit_hash
    )
    assert synced["status"] == "connected", synced
    return marketplace


def rows_of(client: TestClient, headers: dict[str, str], marketplace_id: str) -> dict[str, dict]:
    """Discover rows of one marketplace keyed by name."""
    return plugins_by_name(
        discover_plugins(client, headers, marketplace_id=marketplace_id, limit=200)
    )


def create_second_superuser(
    client: TestClient, superuser_headers: dict[str, str]
) -> tuple[dict, dict[str, str]]:
    email, password = random_email(), random_lower_string()
    r = client.post(
        f"{API}/users/",
        headers=superuser_headers,
        json={"email": email, "password": password, "is_superuser": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_superuser"] is True
    return r.json(), user_authentication_headers(client=client, email=email, password=password)


def snapshots_root() -> Path:
    return Path(settings.SKILL_STORAGE_DIR) / "marketplace-skills"


def entry_dir(marketplace_id: str, plugin_id: str) -> Path:
    return snapshots_root() / marketplace_id / plugin_id


def archive_file(marketplace_id: str, plugin_id: str, sha256: str) -> Path:
    return entry_dir(marketplace_id, plugin_id) / f"{sha256}.tar.gz"


def files_under(marketplace_id: str) -> list[Path]:
    base = snapshots_root() / marketplace_id
    return sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []


def tar_members(data: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        return tf.getmembers()


def get_archive(
    client: TestClient,
    headers: dict[str, str],
    plugin_id: str,
    *,
    sha256: str | None = None,
):
    params = {"sha256": sha256} if sha256 is not None else {}
    return client.get(
        f"{PLUGINS_BASE}/plugins/{plugin_id}/skill-archive", headers=headers, params=params
    )


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
