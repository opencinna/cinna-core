"""K4 — the MCP directory admin routes and the opencinna import.

Scenarios:
  1. Every route is superuser-only (a normal user gets 403).
  2. Create / update / delete lifecycle with validation (http without url,
     stdio without package, ``http:`` url, bad env name, an env item carrying a
     value, bad slug) and 409 ``name_taken``.
  3. Ban hides an entry from the directory but not from the admin list; unban
     restores it.
  4. Import (index fetch stubbed): created / updated / unchanged, ``no_remote``,
     ``name_taken`` vs a manual row, ``stale`` without deletes, idempotence.
  5. Import refusals: ``version: 2`` -> 422; fetch failure / oversized /
     non-JSON bodies -> 502 (the httpx client is faked, never the network).
"""
import json
import uuid
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.utils.addons import (
    DIRECTORY_URL,
    MCP_DIR,
    create_mcp_entry,
    directory_entry,
    directory_names,
    get_directory,
    import_mcp_index,
    list_mcp_entries,
    mcp_http_body,
    opencinna_server,
    set_mcp_access,
)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _code(response) -> str:
    return response.json()["detail"]["code"]


def test_every_route_is_superuser_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    entry = create_mcp_entry(client, superuser_token_headers, mcp_http_body(_slug("guard")))
    u = normal_user_token_headers
    eid = entry["id"]
    calls = [
        client.get(f"{MCP_DIR}/entries", headers=u),
        client.post(f"{MCP_DIR}/entries", headers=u, json=mcp_http_body(_slug("x"))),
        client.put(f"{MCP_DIR}/entries/{eid}", headers=u, json={"title": "t"}),
        client.delete(f"{MCP_DIR}/entries/{eid}", headers=u),
        client.put(f"{MCP_DIR}/entries/{eid}/access", headers=u, json={"access": "banned"}),
        client.post(f"{MCP_DIR}/import", headers=u),
    ]
    assert [c.status_code for c in calls] == [403] * 6
    assert client.get(f"{MCP_DIR}/entries").status_code in (401, 403)
    # nothing changed
    assert [e["name"] for e in list_mcp_entries(client, superuser_token_headers)] == [entry["name"]]


def test_entry_lifecycle_validation_and_name_collisions(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    name = _slug("life")

    # ── Create: a valid http entry and a valid stdio entry
    entry = create_mcp_entry(
        client, h,
        mcp_http_body(name, auth="bearer", categories=["dev"], publisher_name="Acme",
                      env=[{"name": "API_KEY", "secret": True, "description": "key"}]),
    )
    assert entry["name"] == name and entry["source"] == "manual"
    assert entry["access"] == "default" and entry["transport"] == "http"
    stdio_name = _slug("stdio")
    stdio = create_mcp_entry(client, h, {
        "name": stdio_name, "title": "Stdio", "transport": "stdio",
        "package": {"registry": "npm", "name": "@acme/server", "version": "1.0.0", "args": ["--x"]},
    })
    assert stdio["package"]["name"] == "@acme/server"

    # ── Directory reflects both
    listed = get_directory(client, h, kind="mcp")
    assert {name, stdio_name} <= set(directory_names(listed))
    http_entry = directory_entry(listed, name)
    assert http_entry["install"]["mcp"]["auth"] == "bearer"
    assert http_entry["install"]["mcp"]["env"][0]["name"] == "API_KEY"
    assert directory_entry(listed, stdio_name)["install"]["mcp"]["package"]["registry"] == "npm"

    # ── Duplicate name
    dup = client.post(f"{MCP_DIR}/entries", headers=h, json=mcp_http_body(name))
    assert dup.status_code == 409 and _code(dup) == "name_taken"

    # ── Validation
    def post(body: dict):
        return client.post(f"{MCP_DIR}/entries", headers=h, json=body)

    for bad in (
        {**mcp_http_body(_slug("v")), "url": None},
        mcp_http_body(_slug("v"), url="http://insecure.example.com/mcp"),
        {"name": _slug("v"), "title": "t", "transport": "stdio"},
        mcp_http_body(_slug("v"), package={"registry": "npm", "name": "x"}),
        mcp_http_body(_slug("v"), env=[{"name": "lower_case"}]),
    ):
        r = post(bad)
        assert r.status_code == 422, (bad, r.text)
    # An env item is a name only: a stray value is never stored or echoed.
    leaky = post(mcp_http_body(_slug("leak"), env=[{"name": "API_KEY", "value": "s3cret"}]))
    assert "s3cret" not in leaky.text
    assert "s3cret" not in json.dumps(list_mcp_entries(client, h))
    assert "s3cret" not in json.dumps(get_directory(client, h))
    # the domain-level ones carry the code
    r = post(mcp_http_body(_slug("v"), url="http://insecure.example.com/mcp"))
    assert _code(r) == "invalid_entry"
    r = post({"name": _slug("v"), "title": "t", "transport": "stdio"})
    assert _code(r) == "invalid_entry"
    for bad_name in ("Upper", "-lead", "has space", "x" * 65, ""):
        assert post(mcp_http_body(bad_name)).status_code == 422, bad_name
    assert post({**mcp_http_body(_slug("v")), "transport": "ftp"}).status_code == 422

    # ── Update: partial change, validation, unknown id, rename collision
    updated = client.put(f"{MCP_DIR}/entries/{entry['id']}", headers=h,
                         json={"title": "Renamed", "description": "new text"})
    assert updated.status_code == 200
    assert updated.json()["title"] == "Renamed" and updated.json()["url"] == entry["url"]
    assert directory_entry(get_directory(client, h, search=name), name)["title"] == "Renamed"
    bad_update = client.put(f"{MCP_DIR}/entries/{entry['id']}", headers=h,
                            json={"url": "http://insecure.example.com"})
    assert bad_update.status_code == 422 and _code(bad_update) == "invalid_entry"
    assert next(e for e in list_mcp_entries(client, h) if e["id"] == entry["id"])["url"] == entry["url"]
    ghost = str(uuid.uuid4())
    for r in (
        client.put(f"{MCP_DIR}/entries/{ghost}", headers=h, json={"title": "t"}),
        client.delete(f"{MCP_DIR}/entries/{ghost}", headers=h),
        client.put(f"{MCP_DIR}/entries/{ghost}/access", headers=h, json={"access": "banned"}),
    ):
        assert r.status_code == 404 and _code(r) == "entry_not_found"

    # ── Explicit null on a required field is a 422, never a 500
    for field in ("title", "auth", "categories", "env", "verified"):
        r = client.put(f"{MCP_DIR}/entries/{entry['id']}", headers=h, json={field: None})
        assert r.status_code == 422 and _code(r) == "invalid_entry", (field, r.text)

    # ── Delete
    assert client.delete(f"{MCP_DIR}/entries/{entry['id']}", headers=h).status_code == 200
    assert name not in directory_names(get_directory(client, h))
    assert entry["id"] not in {e["id"] for e in list_mcp_entries(client, h)}
    again = client.delete(f"{MCP_DIR}/entries/{entry['id']}", headers=h)
    assert again.status_code == 404
    # the name is free again
    create_mcp_entry(client, h, mcp_http_body(name))


def test_ban_hides_from_the_directory_but_not_from_admin_and_unban_restores(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    h, u = superuser_token_headers, normal_user_token_headers
    name = _slug("banme")
    entry = create_mcp_entry(client, h, mcp_http_body(name))
    assert name in directory_names(get_directory(client, u))

    banned = set_mcp_access(client, h, entry["id"], "banned", reason="abuse")
    assert banned["access"] == "banned" and banned["access_reason"] == "abuse"
    assert name not in directory_names(get_directory(client, u))
    assert name not in directory_names(get_directory(client, h))
    admin_row = next(e for e in list_mcp_entries(client, h) if e["id"] == entry["id"])
    assert admin_row["access"] == "banned" and admin_row["access_reason"] == "abuse"

    unbanned = set_mcp_access(client, h, entry["id"], "default")
    assert unbanned["access"] == "default" and unbanned["access_reason"] is None
    assert name in directory_names(get_directory(client, u))
    bad = client.put(f"{MCP_DIR}/entries/{entry['id']}/access", headers=h,
                     json={"access": "sideways"})
    assert bad.status_code == 422


def test_import_creates_updates_skips_and_reports_stale_without_deleting(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    a, b, c, gone, manual = (_slug(p) for p in ("ia", "ib", "ic", "igone", "iman"))
    no_remote, http_only = _slug("noremote"), _slug("httponly")
    manual_entry = create_mcp_entry(client, h, mcp_http_body(manual, title="Mine"))

    index = {"version": 1, "servers": [
        opencinna_server(a, remotes=[{"type": "streamable-http", "url": f"https://{a}.example.com/mcp", "requiresAuth": True}]),
        opencinna_server(b, remotes=[{"type": "sse", "url": f"https://{b}.example.com/sse"}]),
        opencinna_server(gone),
        opencinna_server(no_remote, remotes=[]),
        opencinna_server(http_only, remotes=[{"type": "streamable-http", "url": "http://plain.example.com/mcp"}]),
        opencinna_server(manual),
    ]}
    first = import_mcp_index(client, h, index)
    assert first["created"] == 3 and first["updated"] == 0 and first["unchanged"] == 0
    assert {(s["id"], s["reason"]) for s in first["skipped"]} == {
        (no_remote, "no_remote"), (http_only, "no_remote"), (manual, "name_taken"),
    }
    assert first["stale"] == []

    rows = {e["name"]: e for e in list_mcp_entries(client, h)}
    assert rows[a]["source"] == "opencinna" and rows[a]["transport"] == "http"
    assert rows[a]["auth"] == "oauth" and rows[a]["verified"] is True
    assert rows[a]["publisher_name"] == "Cinna" and rows[a]["url"] == f"https://{a}.example.com/mcp"
    assert rows[a]["homepage"] == f"https://{a}.example.com"
    assert rows[b]["transport"] == "sse" and rows[b]["auth"] == "none"
    assert rows[manual]["source"] == "manual" and rows[manual]["title"] == "Mine"
    assert no_remote not in rows and http_only not in rows
    in_dir = get_directory(client, h, kind="mcp")
    assert {a, b, gone, manual} <= set(directory_names(in_dir))

    # ── Idempotent: the same index again changes nothing
    second = import_mcp_index(client, h, index)
    assert second["created"] == 0 and second["updated"] == 0 and second["unchanged"] == 3
    assert get_directory(client, h)["etag"] == in_dir["etag"]

    # ── Changed index: a updated, c created, gone no longer listed (stale), b same
    changed = {"version": 1, "servers": [
        opencinna_server(a, remotes=[{"type": "streamable-http", "url": f"https://{a}.example.com/v2"}], title="A v2"),
        opencinna_server(b, remotes=[{"type": "sse", "url": f"https://{b}.example.com/sse"}]),
        opencinna_server(c),
    ]}
    third = import_mcp_index(client, h, changed)
    assert (third["created"], third["updated"], third["unchanged"]) == (1, 1, 1)
    assert third["stale"] == [gone] and third["skipped"] == []
    rows = {e["name"]: e for e in list_mcp_entries(client, h)}
    assert rows[a]["title"] == "A v2" and rows[a]["url"].endswith("/v2") and rows[a]["auth"] == "none"
    assert gone in rows, "import never deletes"
    assert gone in directory_names(get_directory(client, h))
    assert get_directory(client, h)["etag"] != in_dir["etag"]

    # ── A manual entry of the same name is never overwritten by a later import
    assert rows[manual]["id"] == manual_entry["id"]
    # ── A banned imported entry stays banned across an import
    set_mcp_access(client, h, rows[b]["id"], "banned")
    import_mcp_index(client, h, changed)
    assert b not in directory_names(get_directory(client, h))
    assert next(e for e in list_mcp_entries(client, h) if e["name"] == b)["access"] == "banned"


def test_imported_row_that_lost_its_https_remote_is_reported_stale(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    a = _slug("lost")
    import_mcp_index(client, h, {"version": 1, "servers": [opencinna_server(a)]})
    result = import_mcp_index(client, h, {"version": 1, "servers": [opencinna_server(a, remotes=[])]})
    assert result["skipped"] == [{"id": a, "reason": "no_remote"}]
    assert result["stale"] == [a]
    assert a in directory_names(get_directory(client, h)), "never deleted"


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        step = 64 * 1024
        for i in range(0, len(self._body), step):
            yield self._body[i : i + step]


def _fake_client(response: _FakeResponse | Exception):
    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, headers=None):
            if isinstance(response, Exception):
                raise response
            return response

    return _Client


def test_import_refusals_map_to_the_documented_errors(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers

    # version 2 -> 422 (payload stub)
    bad_version = import_mcp_index(client, h, {"version": 2, "servers": []}, expected_status=422)
    assert bad_version["detail"]["code"] == "index_version_unsupported"
    # servers not a list -> 502 index_invalid
    invalid = import_mcp_index(client, h, {"version": 1, "servers": "nope"}, expected_status=502)
    assert invalid["detail"]["code"] == "index_invalid"

    # Real fetch path with a faked httpx client
    target = "app.services.mcp.mcp_directory_service.httpx.Client"
    cases = [
        (_FakeResponse(500, b"boom"), "index_fetch_failed"),
        (httpx.ConnectError("down"), "index_fetch_failed"),
        (httpx.ReadTimeout("slow"), "index_fetch_failed"),
        (_FakeResponse(200, b"x" * (1024 * 1024 + 10)), "index_too_large"),
        (_FakeResponse(200, b"<html>not json"), "index_invalid"),
        (_FakeResponse(200, b"[1, 2]"), "index_invalid"),
    ]
    for response, code in cases:
        with patch(target, _fake_client(response)):
            resp = client.post(f"{MCP_DIR}/import", headers=h)
        assert resp.status_code == 502, (code, resp.text)
        assert _code(resp) == code

    # And the happy path through the same faked client
    name = _slug("viahttp")
    body = json.dumps({"version": 1, "servers": [opencinna_server(name)]}).encode()
    with patch(target, _fake_client(_FakeResponse(200, body))):
        ok = client.post(f"{MCP_DIR}/import", headers=h)
    assert ok.status_code == 200 and ok.json()["created"] == 1
    assert directory_entry(get_directory(client, h, search=name), name)["kind"] == "mcp"
    assert DIRECTORY_URL
