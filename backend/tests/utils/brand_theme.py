"""Brand theme API helpers: package builders from the shared fixtures and
thin wrappers over the admin and public routes."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR
PUBLIC_URL = f"{API}/brand-theme"
ADMIN_URL = f"{API}/admin/brand-theme"
DISCOVERY_URL = "/.well-known/cinna-desktop"

FIXTURES = Path(__file__).resolve().parents[2] / "app" / "services" / "brand_theme" / "fixtures"


def fixture_files(rel: str) -> tuple[bytes, dict[str, bytes]]:
    """``(theme.json bytes, {"assets/<name>": bytes})`` of a fixture package."""
    root = FIXTURES / rel
    assets = {}
    if (root / "assets").is_dir():
        assets = {f"assets/{p.name}": p.read_bytes() for p in sorted((root / "assets").iterdir())}
    return (root / "theme.json").read_bytes(), assets


def fixture_zip(rel: str, *, folder: str | None = None) -> bytes:
    theme, assets = fixture_files(rel)
    prefix = f"{folder}/" if folder else ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(prefix + "theme.json", theme)
        for path, data in assets.items():
            zf.writestr(prefix + path, data)
    return buf.getvalue()


def theme_with(rel: str, **changes: Any) -> bytes:
    """A fixture's theme.json with top-level keys replaced."""
    theme, _ = fixture_files(rel)
    doc = json.loads(theme)
    doc.update(changes)
    return json.dumps(doc).encode()


def upload_zip(client: TestClient, headers: dict[str, str], data: bytes) -> Any:
    return client.post(
        f"{ADMIN_URL}/drafts",
        headers=headers,
        files={"package": ("theme.zip", data, "application/zip")},
    )


def upload_files(
    client: TestClient, headers: dict[str, str], theme: bytes, assets: dict[str, bytes]
) -> Any:
    files = [("theme", ("theme.json", theme, "application/json"))]
    files += [("files", (path.split("/")[-1], data, "application/octet-stream")) for path, data in assets.items()]
    return client.post(f"{ADMIN_URL}/drafts", headers=headers, files=files)


def list_drafts(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    r = client.get(f"{ADMIN_URL}/drafts", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def publish(client: TestClient, headers: dict[str, str], theme_id: str) -> Any:
    return client.post(f"{ADMIN_URL}/drafts/{theme_id}/publish", headers=headers)


def unpublish(client: TestClient, headers: dict[str, str]) -> None:
    r = client.post(f"{ADMIN_URL}/unpublish", headers=headers)
    assert r.status_code == 204, r.text


def delete_draft(client: TestClient, headers: dict[str, str], theme_id: str) -> Any:
    return client.delete(f"{ADMIN_URL}/drafts/{theme_id}", headers=headers)
