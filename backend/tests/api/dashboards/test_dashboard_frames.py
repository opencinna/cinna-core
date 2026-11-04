"""Dashboard frame content host ``/d/v/<cap>/`` over HTTP (P3).

Scenarios:
  1. A frame minted for a block or for an agent preview serves the entry page
     with the kit bridge injected (once, before the page's script) and the
     sandbox CSP; the bridge bytes equal the kit file.
  2. A non-entry file is served as is (no injection); HEAD works.
  3. Refusals: 405 on writes, 404 for ``api/x`` and dot segments, a missing
     file (404, not 409), an artifact cap, a garbage cap.
  4. Ownership is re-checked per request: a cap whose block or agent belongs
     to someone else answers 404.
  5. A stopped environment answers 503 with the error headers.
"""
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.services.environments.platform_protocol_files import kit_snapshot_dir
from tests.utils.artifacts import cap_of, content_path, publish, view
from tests.utils.dashboard_components import (
    create_component_tile,
    make_component,
    patch_environment_adapter,  # noqa: F401  (autouse override of the persistent adapter)
    setup_component_agent,
)
from tests.utils.environment import set_environment_status
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR

PAGE = b"<!doctype html><html><head><title>t</title></head><body><script src=app.js></script></body></html>"
APP_JS = b"console.log('tile')"


def _bridge() -> bytes:
    return (kit_snapshot_dir(Path(settings.ENV_TEMPLATES_DIR)) / "protocols/dashboards-bridge.js").read_bytes()


def _html_component(name: str = "page") -> dict:
    return make_component(name, view="html", entry="index.html", endpoints={})


def _files(component: str = "page") -> dict:
    return {
        (component, None, "index.html"): ("text/html; charset=utf-8", PAGE),
        (component, None, "app.js"): ("text/javascript", APP_JS),
    }


def _tile(client, db, headers, adapter):
    adapter.dashboard_files = _files()
    return create_component_tile(client, db, headers, adapter, [_html_component()])


def _mint_block_cap(client, headers, tile) -> str:
    r = client.post(f"{tile['base']}/frame", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["frame_url"]


def _cap_of_frame_url(url: str) -> str:
    return content_path(url).split("/")[3]


def _assert_error_headers(r) -> None:
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox ") and "default-src 'none'" in csp
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


# ── Scenario 1: entry page gets the bridge ──────────────────────────


def test_block_frame_entry_page_is_bridged_with_sandbox_csp(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter._test_adapter)
    url = _mint_block_cap(client, superuser_token_headers, t)
    cap = _cap_of_frame_url(url)

    r = client.get(content_path(url))
    assert r.status_code == 200, r.text
    wrapped = b"<script>" + _bridge() + b"</script>"
    assert r.content.count(wrapped) == 1
    assert r.content.replace(wrapped, b"") == PAGE
    assert r.content.index(b"<head>") + len(b"<head>") == r.content.index(wrapped)
    assert r.headers["content-type"].startswith("text/html")

    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts allow-forms allow-modals; default-src 'none'; ")
    assert f"/d/v/{cap}/" in csp and "allow-same-origin" not in csp
    assert "frame-src 'none'" in csp and "form-action 'none'" in csp
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_agent_preview_frame_entry_page_is_bridged(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = patch_environment_adapter._test_adapter
    adapter.dashboard_files = _files()
    agent = setup_component_agent(client, db, superuser_token_headers, adapter, [_html_component()])
    r = client.post(
        f"{API}/agents/{agent['id']}/dashboards/page/frame", headers=superuser_token_headers, json={}
    )
    assert r.status_code == 200, r.text
    r = client.get(content_path(r.json()["frame_url"]))
    assert r.status_code == 200, r.text
    assert b"<script>" + _bridge() + b"</script>" in r.content
    assert r.headers["content-security-policy"].startswith("sandbox ")


# ── Scenario 2: other files ─────────────────────────────────────────


def test_non_entry_file_is_served_without_injection_and_head_works(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter._test_adapter)
    entry = content_path(_mint_block_cap(client, superuser_token_headers, t))
    js = entry.rsplit("/", 1)[0] + "/app.js"

    r = client.get(js)
    assert r.status_code == 200
    assert r.content == APP_JS
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["content-security-policy"].startswith("sandbox ")

    h = client.head(entry)
    assert h.status_code == 200 and h.content == b""


# ── Scenario 3: refusals ────────────────────────────────────────────


def test_writes_are_405_with_error_headers(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter._test_adapter)
    entry = content_path(_mint_block_cap(client, superuser_token_headers, t))
    for method in ("post", "put", "patch", "delete"):
        r = getattr(client, method)(entry)
        assert r.status_code == 405, method
        assert r.headers["allow"] == "GET, HEAD"
        _assert_error_headers(r)


def test_api_and_dot_segment_paths_are_404(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = patch_environment_adapter._test_adapter
    t = _tile(client, db, superuser_token_headers, adapter)
    adapter.dashboard_files[("page", None, "api/x")] = ("text/plain", b"leak")
    base = content_path(_mint_block_cap(client, superuser_token_headers, t)).rsplit("/", 1)[0]

    for path in ("api/x", "api/", "%2e%2e/x", "a/%2e%2e/index.html", "%2e/index.html", "a//b", "..%2fx"):
        r = client.get(f"{base}/{path}")
        assert r.status_code == 404, path
        assert b"leak" not in r.content
        _assert_error_headers(r)


def test_missing_file_is_404_not_409(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter._test_adapter)
    base = content_path(_mint_block_cap(client, superuser_token_headers, t)).rsplit("/", 1)[0]
    r = client.get(f"{base}/nope.css")
    assert r.status_code == 404
    _assert_error_headers(r)


def test_artifact_cap_and_garbage_cap_are_404_at_d(
    client: TestClient, superuser_token_headers: dict
) -> None:
    art = publish(client, superuser_token_headers, files={"index.html": b"<html></html>"})
    v = view(client, superuser_token_headers, art["id"])
    cap = cap_of(v)
    assert client.get(f"/a/v/{cap}/index.html").status_code == 200

    r = client.get(f"/d/v/{cap}/index.html")
    assert r.status_code == 404
    _assert_error_headers(r)
    r = client.get("/d/v/invalid/x")
    assert r.status_code == 404
    _assert_error_headers(r)


# ── Scenario 4: ownership ───────────────────────────────────────────


def test_block_cap_of_another_users_dashboard_is_404(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    """A cap minted for user B naming A's block does not resolve."""
    adapter = patch_environment_adapter._test_adapter
    t = _tile(client, db, superuser_token_headers, adapter)
    own = content_path(_mint_block_cap(client, superuser_token_headers, t))
    assert client.get(own).status_code == 200

    # The other user cannot mint for A's block ...
    _, other = create_random_user_with_headers(client)
    assert client.post(f"{t['base']}/frame", headers=other).status_code == 404

    # ... and a cap forged for B over A's block is refused at the content route.
    from app.services.dashboards import frame_cap  # signing helper only; no DB access
    import uuid

    user_id = uuid.UUID(client.get(f"{API}/users/me", headers=other).json()["id"])
    forged, _ = frame_cap.mint(user_id, frame_cap.block_target(uuid.UUID(t["block"]["id"])))
    r = client.get(f"/d/v/{forged}/index.html")
    assert r.status_code == 404
    _assert_error_headers(r)


def test_preview_cap_of_another_users_agent_is_404(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    import uuid

    from app.services.dashboards import frame_cap

    adapter = patch_environment_adapter._test_adapter
    adapter.dashboard_files = _files()
    agent = setup_component_agent(client, db, superuser_token_headers, adapter, [_html_component()])
    other_user, other = create_random_user_with_headers(client)
    forged, _ = frame_cap.mint(
        uuid.UUID(other_user["id"]),
        frame_cap.component_target(uuid.UUID(agent["id"]), None, "page"),
    )
    r = client.get(f"/d/v/{forged}/index.html")
    assert r.status_code == 404
    _assert_error_headers(r)
    r = client.post(
        f"{API}/agents/{agent['id']}/dashboards/page/frame", headers=other, json={}
    )
    assert r.status_code == 404


# ── Scenario 5: stopped environment ─────────────────────────────────


def test_stopped_environment_is_503_with_error_headers_and_no_activation(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter._test_adapter)
    entry = content_path(_mint_block_cap(client, superuser_token_headers, t))
    env_id = t["agent"]["active_environment_id"]

    for status in ("stopped", "suspended"):
        set_environment_status(db, env_id, status)
        r = client.get(entry)
        assert r.status_code == 503, status
        _assert_error_headers(r)
        assert b"<script>" + _bridge() not in r.content
        agent = client.get(f"{API}/agents/{t['agent']['id']}", headers=superuser_token_headers).json()
        assert agent["active_environment_id"] == env_id

    set_environment_status(db, env_id, "running")
    assert client.get(entry).status_code == 200
