"""Artifacts through the account CLI token (P3, D12).

Scenario: with an account token, create (multipart) → republish → download
the zip on the dedicated ``/cli/account/artifacts`` routes; the JSON list and
detail work through the api-proxy (link codes blanked); link creation is
refused by the proxy and served by its own account route; a user JWT is
refused on the account routes.
"""
import io
import zipfile

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.artifacts import build_zip
from tests.utils.cli import account_cli_headers, bootstrap_account_token

API = settings.API_V1_STR


def test_account_token_publish_republish_download(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    account_jwt, _ = bootstrap_account_token(client, superuser_token_headers)
    acc = account_cli_headers(account_jwt)

    # ── Phase 1: create ──
    r = client.post(
        f"{API}/cli/account/artifacts",
        headers=acc,
        data={"title": "From the CLI", "source_label": "laptop:~/out"},
        files=[("archive", ("out.zip", build_zip({"index.html": "<p>1</p>", ".env": "x"}), "application/zip"))],
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["source_kind"] == "cli"
    assert created["excluded"] == [".env"]

    # ── Phase 2: republish ──
    r = client.post(
        f"{API}/cli/account/artifacts/{created['id']}/revisions",
        headers=acc,
        data={"note": "v2"},
        files=[("archive", ("out.zip", build_zip({"index.html": "<p>2</p>"}), "application/zip"))],
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision_number"] == 2

    # ── Phase 3: download ──
    r = client.get(f"{API}/cli/account/artifacts/{created['id']}/download", headers=acc)
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.read("index.html") == b"<p>2</p>"
    r = client.get(f"{API}/cli/account/artifacts/{created['id']}/download?rev=1", headers=acc)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.read("index.html") == b"<p>1</p>"

    # ── Phase 4: JSON verbs through the api-proxy ──
    r = client.post(f"{API}/cli/account/api-proxy", headers=acc, json={"method": "GET", "path": "artifacts"})
    assert r.status_code == 200, r.text
    assert created["id"] in [a["id"] for a in r.json()["data"]]
    r = client.post(
        f"{API}/cli/account/api-proxy",
        headers=acc,
        json={"method": "GET", "path": f"artifacts/{created['id']}"},
    )
    assert r.status_code == 200 and r.json()["revision_count"] == 2

    # ── Phase 5: links — never through the proxy, only the dedicated route ──
    r = client.post(
        f"{API}/cli/account/api-proxy",
        headers=acc,
        json={"method": "POST", "path": f"artifacts/{created['id']}/links", "json_body": {"with_code": True}},
    )
    assert r.status_code == 403, r.text
    r = client.post(f"{API}/cli/account/artifacts/{created['id']}/links", headers=acc, json={"with_code": True})
    assert r.status_code == 200, r.text
    link = r.json()
    assert link["token"] and len(link["code"]) == 4
    # the detail through the proxy carries the link but never its code
    r = client.post(
        f"{API}/cli/account/api-proxy",
        headers=acc,
        json={"method": "GET", "path": f"artifacts/{created['id']}"},
    )
    proxied_links = r.json()["links"]
    assert [x["id"] for x in proxied_links] == [link["id"]]
    assert proxied_links[0]["code"] is None and proxied_links[0]["needs_code"] is True
    direct = client.get(f"{API}/artifacts/{created['id']}", headers=superuser_token_headers).json()
    assert direct["links"][0]["code"] == link["code"]

    # ── Phase 6: the account routes want the account token ──
    r = client.get(f"{API}/cli/account/artifacts/{created['id']}/download", headers=superuser_token_headers)
    assert r.status_code == 401
