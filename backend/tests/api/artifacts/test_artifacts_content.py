"""Artifact content route (``/a/``): headers, resolution, caps, CORS (P2).

Scenarios:
  1. A published site is served file by file from its view cap with the exact
     sandbox CSP and the security headers; MIME from the table; ``dir/`` ⇒
     ``index.html``; HEAD; Range ⇒ 206; ``?dl=1``; ``/a/z/`` zip.
  2. Traversal (plain and encoded), a path not in the manifest, an expired cap,
     a cap with the wrong audience, a user JWT, a cap for another revision and
     a cap pairing one artifact with another's revision ⇒ 404 with the
     security headers and ``no-store``; so does an unmatched ``/a/`` path.
  3. CORS: ``Origin: null`` and a foreign origin get ``ACAO: *`` without
     credentials (the global CORSMiddleware does not leak through); preflight
     is answered on ``/a/``; the API keeps its own CORS.
  4. A cap is not a login token (``/users/me`` refuses it).
  5. Access is re-checked per request (D1): another user cannot mint a view;
     deactivating the owner kills the owner's live cap, reactivating revives
     it; deleting the owner kills it for good and removes the files.
  6. Upload shapes of the thin ``POST /artifacts``: files[] with relative
     names, a single HTML file, archive xor files, ``core_agent`` refused,
     a rejected zip, the quota.

Unit tests for the CSP string, the cap codec and path containment live in
``tests/unit/test_artifact_content_headers.py``; ingest rules in
``tests/unit/test_artifact_ingest.py``.
"""
import hashlib
import io
import uuid
import zipfile
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.artifacts import (
    build_symlink_zip,
    build_zip,
    cap_of,
    content_path,
    publish,
    view,
)
from tests.utils.platform_token import mint_platform_token
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR

INDEX_HTML = b"<!doctype html><script type=module src=app.js></script>"
APP_JS = b"console.log('hello')"
SQLITE = b"SQLite format 3\x00" + bytes(range(256)) * 4
WASM = b"\x00asm\x01\x00\x00\x00"


def _site_zip() -> bytes:
    return build_zip(
        {
            "index.html": INDEX_HTML,
            "app.js": APP_JS,
            "data/data.sqlite": SQLITE,
            "lib/sql.wasm": WASM,
            "docs/index.html": b"<p>docs</p>",
            ".env": b"SECRET=1",
        }
    )


def _expected_csp(base: str) -> str:
    parts = urlsplit(settings.FRONTEND_HOST)
    ancestor = f"{parts.scheme}://{parts.netloc}"
    return (
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox "
        "allow-downloads allow-modals allow-forms; "
        "default-src 'none'; "
        f"script-src {base} 'unsafe-inline' 'wasm-unsafe-eval' blob:; "
        f"style-src {base} 'unsafe-inline'; "
        f"img-src {base} data: blob:; "
        f"font-src {base} data:; "
        f"connect-src {base} data: blob:; "
        f"media-src {base} data: blob:; "
        "worker-src blob:; "
        f"frame-src {base}; "
        "form-action 'none'; "
        "base-uri 'none'; "
        f"frame-ancestors {ancestor}"
    )


def _assert_security_headers(r) -> None:
    csp = r.headers.get("content-security-policy", "")
    assert csp.startswith("sandbox allow-scripts "), csp
    assert "allow-same-origin" not in csp
    assert "frame-ancestors " in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers
    assert "cross-origin-resource-policy" not in r.headers


def _assert_404(r) -> None:
    assert r.status_code == 404, (r.status_code, r.text[:200])
    _assert_security_headers(r)
    assert r.headers["cache-control"] == "no-store"


def _forged_cap(artifact_id: str, revision_id: str, via: str, *, aud: str = "artifact_view", hours: float = 1) -> str:
    return mint_platform_token(
        artifact_id,
        expires_delta=timedelta(hours=hours),
        extra_claims={"aud": aud, "rev": revision_id, "via": via},
    )


def test_site_is_served_with_security_headers(client: TestClient) -> None:
    owner, headers = create_random_user_with_headers(client)

    # ── Phase 1: publish; the create response carries the summary + excluded ──
    created = publish(client, headers, archive=_site_zip(), title="Dashboard")
    assert created["kind"] == "site"
    assert created["entry_path"] == "index.html"
    assert created["visibility"] == "private"
    assert created["revision_number"] == 1
    assert created["current_revision_number"] == 1
    assert created["revision_count"] == 1
    assert created["file_count"] == 5
    assert created["excluded"] == [".env"]
    assert created["grant_count"] == 0
    assert created["live_link_count"] == 0
    assert created["is_owner"] is True
    assert created["source_kind"] == "upload"
    assert created["url"].endswith(f"/artifact/{created['id']}")

    # ── Phase 2: mint a view ──
    v = view(client, headers, created["id"])
    assert v["entry"] == "index.html"
    assert v["revision_number"] == 1
    assert {f["path"] for f in v["files"]} == {
        "index.html", "app.js", "data/data.sqlite", "lib/sql.wasm", "docs/index.html"
    }
    base = v["content_base"]
    assert base.startswith(settings.artifacts_content_origin + "/a/v/")
    assert base.endswith("/")
    root = content_path(base)

    # ── Phase 3: the root serves index.html with the exact CSP ──
    r = client.get(root)
    assert r.status_code == 200
    assert r.content == INDEX_HTML
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["content-security-policy"] == _expected_csp(base)
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert r.headers["etag"] == f'"{hashlib.sha256(INDEX_HTML).hexdigest()}"'
    _assert_security_headers(r)

    # ── Phase 4: MIME by extension, dir/ ⇒ index.html ──
    assert client.get(root + "app.js").headers["content-type"] == "text/javascript; charset=utf-8"
    assert client.get(root + "lib/sql.wasm").headers["content-type"] == "application/wasm"
    r = client.get(root + "data/data.sqlite")
    assert r.headers["content-type"] == "application/vnd.sqlite3"
    assert r.content == SQLITE
    assert client.get(root + "docs/").content == b"<p>docs</p>"

    # ── Phase 5: HEAD, Range, download ──
    r = client.head(root + "app.js")
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(APP_JS))
    _assert_security_headers(r)
    r = client.get(root + "data/data.sqlite", headers={"Range": "bytes=0-15"})
    assert r.status_code == 206
    assert r.content == SQLITE[:16]
    assert r.headers["content-range"] == f"bytes 0-15/{len(SQLITE)}"
    _assert_security_headers(r)
    r = client.get(root + "app.js?dl=1")
    assert r.headers["content-disposition"].startswith("attachment")
    assert 'filename="app.js"' in r.headers["content-disposition"]

    # ── Phase 6: the cap's zip ──
    r = client.get(content_path(v["download_url"]))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith("attachment")
    _assert_security_headers(r)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == sorted(f["path"] for f in v["files"])
        assert zf.read("app.js") == APP_JS


def test_rejections_are_404_with_headers(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    _, headers = create_random_user_with_headers(client)
    a = publish(client, headers, archive=_site_zip(), title="A")
    b = publish(client, headers, files={"report.html": b"<p>b</p>"}, title="B")
    v = view(client, headers, a["id"])
    root = content_path(v["content_base"])
    cap = cap_of(v)
    owner_via = f"u:{a['owner_id']}"

    # ── Traversal and paths outside the manifest ──
    for path in [
        "%2e%2e/%2e%2e/etc/passwd",
        "docs/..%2findex.html",
        "..%2f..%2f.env",
        ".env",
        "nope.txt",
        "data",
        "%2fetc%2fpasswd",
    ]:
        _assert_404(client.get(root + path))

    # ── Caps that must not resolve ──
    expired = _forged_cap(a["id"], a["current_revision_id"], owner_via, hours=-1)
    wrong_aud = _forged_cap(a["id"], a["current_revision_id"], owner_via, aud="agent_env")
    other_rev = _forged_cap(a["id"], str(uuid.uuid4()), owner_via)
    crossed = _forged_cap(a["id"], b["current_revision_id"], owner_via)
    user_jwt = normal_user_token_headers["Authorization"].split(" ", 1)[1]
    for bad in [expired, wrong_aud, other_rev, crossed, user_jwt, cap[:-4] + "AAAA", "not-a-cap"]:
        _assert_404(client.get(f"/a/v/{bad}/index.html"))
        _assert_404(client.get(f"/a/z/{bad}"))

    # The well-formed forged cap with the real ids does work (the above are not
    # failing for an unrelated reason).
    good = _forged_cap(a["id"], a["current_revision_id"], owner_via)
    assert client.get(f"/a/v/{good}/index.html").status_code == 200

    # ── Unmatched /a/ paths still carry the headers ──
    _assert_404(client.get("/a/unknown"))
    _assert_404(client.get(f"/a/x/{cap}/index.html"))


def test_cors_is_wildcard_without_credentials(client: TestClient) -> None:
    _, headers = create_random_user_with_headers(client)
    a = publish(client, headers, archive=_site_zip())
    root = content_path(view(client, headers, a["id"])["content_base"])

    for origin in ["null", "https://evil.example"]:
        r = client.get(root + "data/data.sqlite", headers={"Origin": origin})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in r.headers
        assert "origin" not in r.headers.get("vary", "").lower()
        exposed = r.headers["access-control-expose-headers"]
        assert "Content-Range" in exposed and "Accept-Ranges" in exposed

        # 404s too.
        r = client.get(root + "missing.js", headers={"Origin": origin})
        _assert_404(r)

    # Preflight is answered on /a/ without credentials.
    r = client.options(
        root + "data/data.sqlite",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "range",
        },
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers
    assert "GET" in r.headers["access-control-allow-methods"]

    # The API keeps the global CORS policy (reflected origin + credentials).
    r = client.get(f"{API}/utils/health-check/", headers={"Origin": "https://evil.example"})
    assert r.headers["access-control-allow-origin"] == "https://evil.example"
    assert r.headers["access-control-allow-credentials"] == "true"


def test_cap_is_not_a_login_token(client: TestClient) -> None:
    _, headers = create_random_user_with_headers(client)
    a = publish(client, headers, archive=_site_zip())
    cap = cap_of(view(client, headers, a["id"]))

    r = client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {cap}"})
    assert r.status_code == 403
    r = client.post(f"{API}/artifacts/{a['id']}/view", headers={"Authorization": f"Bearer {cap}"})
    assert r.status_code == 403


def test_access_is_rechecked_per_request(
    client: TestClient, superuser_token_headers: dict[str, str], artifact_storage
) -> None:
    owner, headers = create_random_user_with_headers(client)
    _, other_headers = create_random_user_with_headers(client)
    a = publish(client, headers, archive=_site_zip())
    url = content_path(view(client, headers, a["id"])["content_base"]) + "index.html"
    assert client.get(url).status_code == 200

    # ── Another user cannot mint a view of a private artifact; unknown ids 404 ──
    view(client, other_headers, a["id"], expect=404)
    view(client, headers, str(uuid.uuid4()), expect=404)
    view(client, headers, a["id"], rev=2, expect=404)
    assert view(client, headers, a["id"], rev=1)["revision_number"] == 1

    # ── Deactivating the owner kills the live cap; reactivating revives it ──
    r = client.patch(f"{API}/users/{owner['id']}", headers=superuser_token_headers, json={"is_active": False})
    assert r.status_code == 200, r.text
    _assert_404(client.get(url))
    r = client.patch(f"{API}/users/{owner['id']}", headers=superuser_token_headers, json={"is_active": True})
    assert r.status_code == 200, r.text
    assert client.get(url).status_code == 200

    # ── Deleting the owner deletes the artifact: the cap dies, the files go ──
    assert (artifact_storage / a["id"]).is_dir()
    r = client.delete(f"{API}/users/me", headers=headers)
    assert r.status_code == 200, r.text
    _assert_404(client.get(url))
    assert not (artifact_storage / a["id"]).exists()


def test_publish_upload_shapes(client: TestClient) -> None:
    _, headers = create_random_user_with_headers(client)

    # files[] keep their relative names; no index.html ⇒ a file set
    created = publish(
        client, headers, files={"report.pdf": b"%PDF-1.4", "sub/data.csv": b"a,b\n1,2"}, title="Files"
    )
    assert created["kind"] == "files"
    assert created["entry_path"] is None
    assert created["file_count"] == 2
    v = view(client, headers, created["id"])
    assert v["entry"] is None
    assert {f["path"] for f in v["files"]} == {"report.pdf", "sub/data.csv"}
    r = client.get(content_path(v["content_base"]) + "sub/data.csv")
    assert r.content == b"a,b\n1,2"
    assert r.headers["content-type"] == "text/csv; charset=utf-8"

    # one HTML file ⇒ a site entered on it
    single = publish(client, headers, files={"report.html": b"<p>r</p>"}, source_kind="cli", source_label="laptop")
    assert (single["kind"], single["entry_path"]) == ("site", "report.html")
    assert (single["source_kind"], single["source_label"]) == ("cli", "laptop")

    # explicit entry path
    entry = publish(client, headers, archive=_site_zip(), entry_path="docs/index.html")
    assert entry["entry_path"] == "docs/index.html"

    # archive xor files; core_agent is not an upload source; blank title
    publish(client, headers, expect=422)
    publish(client, headers, archive=_site_zip(), files={"a.txt": b"x"}, expect=422)
    publish(client, headers, archive=_site_zip(), source_kind="core_agent", expect=422)
    publish(client, headers, archive=_site_zip(), title="  ", expect=422)

    # ingest rejections surface as 400 / 413 with the reason
    r = client.post(
        f"{API}/artifacts",
        headers=headers,
        data={"title": "Bad"},
        files=[("archive", ("bad.zip", build_symlink_zip("assets/link"), "application/zip"))],
    )
    assert r.status_code == 400
    assert "assets/link" in r.json()["detail"]
    publish(client, headers, archive=_site_zip(), entry_path="missing.html", expect=400)

    # quota counts every revision the user owns
    with patch.object(settings, "ARTIFACT_USER_QUOTA_BYTES", 2000):
        r = client.post(
            f"{API}/artifacts",
            headers=headers,
            data={"title": "Too much"},
            files=[("files", ("big.bin", b"x" * 1500, "application/octet-stream"))],
        )
        assert r.status_code == 413, r.text


def test_zip_is_cached_per_revision_and_head_never_builds(client: TestClient, artifact_storage) -> None:
    _, headers = create_random_user_with_headers(client)
    a = publish(client, headers, archive=_site_zip())
    v = view(client, headers, a["id"])
    zip_url = content_path(v["download_url"])
    cached = artifact_storage / a["id"] / f"{a['current_revision_id']}.zip"

    r = client.head(zip_url)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    _assert_security_headers(r)
    assert not cached.exists()

    first = client.get(zip_url)
    assert first.status_code == 200
    assert cached.is_file()
    built_at = cached.stat().st_mtime_ns
    second = client.get(content_path(view(client, headers, a["id"])["download_url"]))
    assert second.content == first.content
    assert cached.stat().st_mtime_ns == built_at
    assert client.head(zip_url).headers["content-length"] == str(cached.stat().st_size)
    assert not list((artifact_storage / ".tmp").iterdir())


def test_nfd_url_and_folder_redirect(client: TestClient) -> None:
    import unicodedata
    from urllib.parse import quote

    _, headers = create_random_user_with_headers(client)
    nfc = unicodedata.normalize("NFC", "café/menu.html")
    a = publish(client, headers, archive=build_zip({nfc: "<p>menu</p>", "docs/index.html": "<p>docs</p>"}))
    root = content_path(view(client, headers, a["id"])["content_base"])

    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc
    assert client.get(root + quote(nfd)).content == b"<p>menu</p>"
    assert client.get(root + quote(nfc)).content == b"<p>menu</p>"

    r = client.get(root + "docs", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "docs/"
    _assert_security_headers(r)
    assert client.get(root + "docs").content == b"<p>docs</p>"
    _assert_404(client.get(root + "nodir", follow_redirects=False))
