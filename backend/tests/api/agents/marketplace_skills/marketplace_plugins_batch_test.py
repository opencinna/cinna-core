"""``GET /llm-plugins/plugins?ids=`` — batch lookup under the `/discover` rule."""
from fastapi.testclient import TestClient

from tests.utils.llm_plugin import PLUGINS_BASE
from tests.utils.marketplace_skills import (
    create_second_superuser,
    new_skills_marketplace,
    rows_of,
    skills_repo,
)

UNKNOWN = "00000000-0000-4000-8000-000000000001"


def _batch(client, headers, ids):
    return client.get(f"{PLUGINS_BASE}/plugins", headers=headers, params={"ids": ",".join(ids)})


def test_batch_returns_only_visible_ids_in_request_order(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Caller owns a private marketplace, another superuser owns a public and a
       private one.
    2. Mixed ids (own, public, foreign-private, unknown, duplicate) → only own
       and public, in request order, once each, with the snapshot fields.
    """
    _, other = create_second_superuser(client, superuser_token_headers)
    own = new_skills_marketplace(client, superuser_token_headers, skills_repo(("own-a",), broken=()), public_discovery=False)
    public = new_skills_marketplace(client, other, skills_repo(("pub-a",), broken=()), public_discovery=True)
    private = new_skills_marketplace(client, other, skills_repo(("priv-a",), broken=()), public_discovery=False)
    own_row = rows_of(client, superuser_token_headers, own["id"])["own-a"]
    pub_row = rows_of(client, other, public["id"])["pub-a"]
    priv_row = rows_of(client, other, private["id"])["priv-a"]

    r = _batch(client, superuser_token_headers, [pub_row["id"], priv_row["id"], UNKNOWN, own_row["id"], pub_row["id"]])

    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["id"] for p in body["data"]] == [pub_row["id"], own_row["id"]]
    assert body["count"] == 2
    for p, src in zip(body["data"], (pub_row, own_row)):
        assert p["skill_archive_sha256"] == src["skill_archive_sha256"]
        assert p["skill_archive_size"] == src["skill_archive_size"]
        assert p["supported"] is True
        assert "unsupported_reason" in p


def test_batch_rejects_too_many_and_malformed_ids(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """101 ids → 422 too_many_ids; a bad uuid → 422 invalid_id; 100 ids are fine."""
    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(101)]
    r = _batch(client, superuser_token_headers, ids)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_many_ids"

    r = _batch(client, superuser_token_headers, ids[:100])
    assert r.status_code == 200 and r.json() == {"data": [], "count": 0}

    r = _batch(client, superuser_token_headers, [ids[0], "not-a-uuid"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_id"


def test_single_plugin_route_is_not_shadowed_by_the_batch_route(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """`GET /plugins/{id}` still resolves next to `GET /plugins?ids=`."""
    m = new_skills_marketplace(client, superuser_token_headers, skills_repo(("solo",), broken=()))
    row = rows_of(client, superuser_token_headers, m["id"])["solo"]
    r = client.get(f"{PLUGINS_BASE}/plugins/{row['id']}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == row["id"]
